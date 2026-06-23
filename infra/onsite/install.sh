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
# Ne touche PAS aux secrets : GATEWAY_KEY dans /etc/alarm-gateway.env, SMTP_* dans
# /opt/alarm/.env.prod.{node<N>,secrets}. Le script signale ce qui manque.
# Cf docs/PROVISIONING_ONSITE.md et docs/FAILOVER_4G.md.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
say(){ echo "[onsite-install] $*"; }
[ "$(id -u)" -eq 0 ] || { echo "À lancer en root (sudo)." >&2; exit 1; }
say "repo = $REPO"

# 1. Scripts d'infra modem + supervision disque -> /opt/alarm/
install -d -m755 /opt/alarm
for s in 4g-standby.sh modem-set-rat.py modem-watchdog.py disk-check.sh send-alert-email.py; do
  install -m755 "$REPO/scripts/$s" "/opt/alarm/$s"; say "/opt/alarm/$s"
done

# 2. Code gateway -> /opt/alarm-gateway/ + accès au port série (dialout)
if id alarm-gateway >/dev/null 2>&1; then
  install -d -o alarm-gateway -g alarm-gateway -m750 /opt/alarm-gateway
  for f in "$REPO"/gateway/*.py; do
    install -o alarm-gateway -g alarm-gateway -m644 "$f" "/opt/alarm-gateway/$(basename "$f")"
  done
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

# 3. Units systemd modem
for u in alarm-4g-standby alarm-modem-rat alarm-modem-watchdog; do
  install -m644 "$REPO/infra/onsite/systemd/$u.service" "/etc/systemd/system/$u.service"; say "unit $u"
done

# 3b. Supervision disque : service oneshot + timer 15 min (garde-fou anti-saturation,
#     cf incident WAL 2026-06-22). Pas de stack modem requise -> tourne aussi sur node3.
for ext in service timer; do
  install -m644 "$REPO/infra/onsite/systemd/alarm-disk-check.$ext" "/etc/systemd/system/alarm-disk-check.$ext"; say "unit alarm-disk-check.$ext"
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
for u in alarm-4g-standby alarm-modem-rat alarm-modem-watchdog; do
  systemctl enable "$u" >/dev/null 2>&1 || true; say "enabled $u"
done
# Le timer disque, lui, on l'active ET on le démarre tout de suite : c'est une
# sonde read-only sans risque, et NE PAS la démarrer reproduirait la classe de bug
# de l'incident (rien ne surveillait). enable --now est idempotent.
systemctl enable --now alarm-disk-check.timer >/dev/null 2>&1 || true; say "enabled+started alarm-disk-check.timer"

say "FAIT. À valider à la main :"
say "  sudo systemctl restart alarm-sms-gateway                         # bascule sur modem_gateway.py"
say "  sudo systemctl start alarm-4g-standby alarm-modem-watchdog       # + alarm-modem-rat tourne au boot"
say "  Pour les alertes email du watchdog : SMTP_* dans /opt/alarm/.env.prod.{node<N>,secrets}"
say "  Smoketest 4G : sudo APN=orange bash scripts/smoketest-4g.sh"
say "  Supervision disque (timer déjà actif) : sudo /opt/alarm/disk-check.sh --dry-run    # seuils, sans email"
say "                                          sudo /opt/alarm/disk-check.sh --test-email  # vérifie la chaîne SMTP"
say "  (mêmes SMTP_* que le watchdog — requis aussi sur node3 : /opt/alarm/.env.prod.{node3,secrets})"
