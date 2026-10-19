#!/usr/bin/env bash
# infra/onsite/install.sh — installe / met à jour la stack modem on-site sur un nœud :
#   - gateway SMS + voix + contact sec  (modem_gateway.py, port AT)
#   - secours internet 4G               (alarm-4g-standby, QMI)
#   - lock RAT WCDMA+LTE sans 2G         (alarm-modem-rat, CNMP=54 au boot)
#   - watchdog modem                     (alarm-modem-watchdog, auto-récup + alerte email)
#   - supervision disque                 (alarm-disk-check.timer, alerte email si fs plein)
#
# Idempotent — sans danger à re-lancer. À exécuter en root depuis la racine d'un
# checkout du repo (typiquement /opt/alarm qui est un checkout git) :
#
#   sudo bash infra/onsite/install.sh
#
# Ensuite, alarm-host-sync (installe ici) le relance tout seul en `--auto` a chaque commit
# promu :stable : une seule execution manuelle par noeud suffit (bootstrap).
#
# Ne touche PAS aux secrets : GATEWAY_KEY dans /etc/alarm-gateway.env, SMTP_* dans
# /opt/alarm/.env.prod.{node<N>,secrets}. Le script signale ce qui manque.
# Cf docs/PROVISIONING_ONSITE.md et docs/FAILOVER_4G.md.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
say(){ echo "[onsite-install] $*"; }
[ "$(id -u)" -eq 0 ] || { echo "À lancer en root (sudo)." >&2; exit 1; }
# --auto : lance par alarm-host-sync (deploiement auto au commit :stable). Ne touche pas au
# code gateway (alarm-gateway-sync le deploie depuis l'image et redemarre la gateway) et
# redemarre les units dont un fichier a change.
AUTO=0; [ "${1:-}" = "--auto" ] && AUTO=1
say "repo = $REPO (auto=$AUTO)"
RESTART=""
# inst <src> <dst> <mode> [unit] : installe si different, et note l'unit a redemarrer.
inst(){ cmp -s "$1" "$2" && return 0; install -m"$3" "$1" "$2"; say "$2"; [ -n "${4:-}" ] && RESTART="$RESTART $4"; return 0; }

# 1. Scripts d'infra modem + supervision disque + bascule box/4G -> /opt/alarm/
install -d -m755 /opt/alarm
for s in 4g-standby.sh:alarm-4g-standby modem-set-rat.py: modem-watchdog.py:alarm-modem-watchdog \
         disk-check.sh: send-alert-email.py: wan-failover.py:alarm-wan-failover; do
  inst "$REPO/scripts/${s%%:*}" "/opt/alarm/${s%%:*}" 755 "${s#*:}"
done

# 1b. Deploiement auto (CD) : code gateway depuis l'image + partie hote au commit :stable.
inst "$REPO/scripts/alarm-gateway-sync.sh" /usr/local/bin/alarm-gateway-sync 755
inst "$REPO/scripts/alarm-host-sync.sh" /usr/local/bin/alarm-host-sync 755

# 2. Code gateway -> /opt/alarm-gateway/ + accès au port série (dialout)
if [ "$AUTO" = 1 ]; then
  say "code gateway : laisse a alarm-gateway-sync (--auto)"
elif id alarm-gateway >/dev/null 2>&1; then
  install -d -o alarm-gateway -g alarm-gateway -m750 /opt/alarm-gateway
  for f in "$REPO"/gateway/*.py; do
    install -o alarm-gateway -g alarm-gateway -m644 "$f" "/opt/alarm-gateway/$(basename "$f")"
  done
  # Messages vocaux des appels d'alarme (INV-132/133)
  install -d -o alarm-gateway -g alarm-gateway -m755 /opt/alarm-gateway/voice
  install -o alarm-gateway -g alarm-gateway -m644 "$REPO"/gateway/voice/*.wav /opt/alarm-gateway/voice/
  say "code gateway -> /opt/alarm-gateway/"
  if id -nG alarm-gateway | grep -qw dialout; then
    say "alarm-gateway déjà dans dialout"
  else
    usermod -aG dialout alarm-gateway
    say "alarm-gateway ajouté à dialout (CRUCIAL — sinon Permission denied sur /dev/ttyUSB)"
  fi
else
  say "ATTENTION: user 'alarm-gateway' absent — gateway non déployée (créer le user d'abord)"
fi

# 3. Units systemd modem + bascule box/4G (INV-140)
for u in alarm-4g-standby alarm-modem-rat alarm-modem-watchdog alarm-wan-failover; do
  inst "$REPO/infra/onsite/systemd/$u.service" "/etc/systemd/system/$u.service" 644 "$u"
done

# 3b. Supervision disque : service oneshot + timer 15 min (garde-fou anti-saturation,
#     cf incident WAL 2026-06-22). Pas de stack modem requise -> tourne aussi sur node3.
# 3c. Timers de deploiement auto (oneshot : pas de redemarrage, le prochain tick prend le neuf).
for f in alarm-disk-check.service alarm-disk-check.timer alarm-gateway-sync.service alarm-gateway-sync.timer \
         alarm-host-sync.service alarm-host-sync.timer; do
  inst "$REPO/infra/onsite/systemd/$f" "/etc/systemd/system/$f" 644
done

# 4. Drop-in gateway : exécuter modem_gateway.py (superset contact+voix+SMS), PAS le
#    legacy sms_gateway.py.
if [ -f /etc/systemd/system/alarm-sms-gateway.service ]; then
  install -d -m755 /etc/systemd/system/alarm-sms-gateway.service.d
  cat > /etc/systemd/system/alarm-sms-gateway.service.d/override.conf <<'OVR'
[Service]
ExecStart=
ExecStart=python3 /opt/alarm-gateway/modem_gateway.py
OVR
  say "drop-in gateway -> modem_gateway.py"
else
  say "ATTENTION: alarm-sms-gateway.service absent — installer l'unit gateway d'abord"
fi

# 5. Variables d'env gateway non-secrètes (idempotent — ne touche pas l'existant)
ENV=/etc/alarm-gateway.env
if [ -f "$ENV" ]; then
  add_env(){ grep -q "^$1=" "$ENV" || { echo "$1=$2" >> "$ENV"; say "env += $1"; }; }
  add_env DRY_CONTACT_ENABLED true
  add_env GATEWAY_ID "$(hostname -s)"
  # 3 backends à interroger (le gateway trouve le leader via /health). IPs Wireguard
  # du mesh : 10.99.0.1=node3(cloud) 10.99.0.2=node1 10.99.0.3=node2.
  add_env NODE1_URL http://10.99.0.1:8000
  add_env NODE2_URL http://10.99.0.2:8000
  add_env NODE3_URL http://10.99.0.3:8000
  grep -q "^GATEWAY_KEY=" "$ENV" || say "ATTENTION: GATEWAY_KEY manquant dans $ENV (doit matcher le backend)"
else
  say "ATTENTION: $ENV absent — la gateway ne démarrera pas"
fi

# 6. Reload + enable (boot). On NE redémarre pas la gateway ici (geste délibéré).
systemctl daemon-reload
# Pile modem seulement sur le noeud qui a la gateway (onsite-2) : ailleurs le watchdog
# alerterait "modem absent" a tort.
if id alarm-gateway >/dev/null 2>&1; then
  for u in alarm-4g-standby alarm-modem-rat alarm-modem-watchdog; do
    systemctl enable "$u" >/dev/null 2>&1 || true; say "enabled $u"
  done
else
  say "pas de user alarm-gateway : pile modem non activee"
fi
# Le timer disque, lui, on l'active ET on le démarre tout de suite : c'est une
# sonde read-only sans risque, et NE PAS la démarrer reproduirait la classe de bug
# de l'incident (rien ne surveillait). enable --now est idempotent.
systemctl enable --now alarm-disk-check.timer >/dev/null 2>&1 || true; say "enabled+started alarm-disk-check.timer"
# Idem : la bascule box/4G ne fait rien sans route 4G (onsite-1) et remet toujours la box
# en priorite a l'arret ; les timers de deploiement auto doivent tourner pour exister.
for u in alarm-wan-failover alarm-gateway-sync.timer alarm-host-sync.timer; do
  systemctl enable --now "$u" >/dev/null 2>&1 || true; say "enabled+started $u"
done

if [ "$AUTO" = 1 ]; then
  # Units dont le script ou le fichier d'unit a change : relance (try-restart = seulement si actives).
  for u in $(echo "$RESTART" | tr ' ' '\n' | sort -u); do
    systemctl try-restart "$u.service" && say "redemarre $u"
  done
  say "FAIT (auto)."
  exit 0
fi

say "FAIT. À valider à la main :"
say "  sudo systemctl restart alarm-sms-gateway                         # bascule sur modem_gateway.py"
say "  sudo systemctl start alarm-4g-standby alarm-modem-watchdog       # + alarm-modem-rat tourne au boot"
say "  Pour les alertes email du watchdog : SMTP_* dans /opt/alarm/.env.prod.{node<N>,secrets}"
say "  Smoketest 4G : sudo APN=orange bash scripts/smoketest-4g.sh"
say "  Supervision disque (timer déjà actif) : sudo /opt/alarm/disk-check.sh --dry-run    # seuils, sans email"
say "                                          sudo /opt/alarm/disk-check.sh --test-email  # vérifie la chaîne SMTP"
say "  (mêmes SMTP_* que le watchdog — requis aussi sur node3 : /opt/alarm/.env.prod.{node3,secrets})"
