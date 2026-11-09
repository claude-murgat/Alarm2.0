#!/usr/bin/env bash
# infra/onsite/install.sh — installe / met à jour la stack modem on-site sur un nœud. Toutes les
# machines sur site l'ont (INV-182) : modem et Arduino se branchent sur n'importe laquelle, et sans
# matériel rien n'échoue ni ne redémarre en boucle (INV-183, INV-184).
#   - gateway SMS + voix + contact sec  (modem_gateway.py, port AT ; unit alarm-sms-gateway)
#   - secours internet 4G               (alarm-4g-standby, QMI)
#   - selection reseau auto              (alarm-modem-rat, CNMP=2 au boot)
#   - watchdog modem                     (alarm-modem-watchdog, auto-récup + alerte email)
#   - supervision disque                 (alarm-disk-check.timer, alerte email si fs plein)
#   - panne controlee « coupure reseau » (alarm-chaos-net + sudoers alarm-cd, INV-177/178)
#   - DNS de secours                     (resolveur public apres celui du LAN, INV-181)
#   - conformite au standard             (alarm-node-check.timer, quotidien, INV-186)
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
# /opt/alarm/.env.prod.{node<N>,secrets}. Le script signale ce qui manque (et GATEWAY_SITE, le
# site de la machine, posé à la main au provisionnement).
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
# Nouvelle logique de sync -> oublier l'image deja traitee pour qu'elle re-compare tout
# (sinon la copie des voix, ajoutee le 2026-10-19, n'aurait lieu qu'au prochain changement d'image).
cmp -s "$REPO/scripts/alarm-gateway-sync.sh" /usr/local/bin/alarm-gateway-sync || rm -f /var/lib/alarm/cd/gateway-image-id
inst "$REPO/scripts/alarm-gateway-sync.sh" /usr/local/bin/alarm-gateway-sync 755
inst "$REPO/scripts/alarm-host-sync.sh" /usr/local/bin/alarm-host-sync 755

# 1c. Panne controlee, mode « coupure reseau » (INV-177/178) : helper root + sudoers qui autorise
# `sudo -n alarm-chaos-net` au user alarm (appel SSH depuis le runner NODE3). Une sudoers cassee
# bloque tout sudo sur le noeud : visudo lint une copie root, et c'est elle qui est posee (ce qui est
# linte est ce qui est pose). visudo refuse -> on saute CETTE etape seulement (l'ancienne reste) :
# un exit ici bloquerait tout le CD hote du noeud (alarm-host-sync reessaierait en vain).
inst "$REPO/scripts/alarm-chaos-net.sh" /usr/local/bin/alarm-chaos-net 755
SUDOERS_TMP=$(mktemp)
cp "$REPO/infra/onsite/sudoers/alarm-cd" "$SUDOERS_TMP"
if visudo -c -q -f "$SUDOERS_TMP"; then
  inst "$SUDOERS_TMP" /etc/sudoers.d/alarm-cd 440
else
  say "ATTENTION : infra/onsite/sudoers/alarm-cd refuse par visudo -c : sudoers NON posee (l'ancienne reste), suite de l'installation"
fi
rm -f "$SUDOERS_TMP"

# 1d. DNS de secours (INV-181) : un resolveur public apres celui du LAN. Sans lui, un noeud passe en
# 4G (box morte) garde le VPN mais perd registre, GitHub et SMTP (alarm-1, 2026-11-02). Un echec
# ici ne doit pas bloquer le reste de l'installation.
bash "$REPO/scripts/dns-fallback.sh" || say "ATTENTION : dns-fallback.sh en echec, DNS de secours non pose"

# 1e. Conformite au standard sur site (INV-186, docs/STANDARD_MACHINE.md) : le controle et les valeurs
# attendues (un seul fichier, infra/onsite/standard.env), lances chaque jour par alarm-node-check.timer.
install -d -m755 /usr/local/share/alarm
inst "$REPO/infra/onsite/standard.env" /usr/local/share/alarm/standard.env 644
inst "$REPO/scripts/alarm-node-check.sh" /usr/local/bin/alarm-node-check 755

# 1f. Copie des sauvegardes de la base envoyee par NODE3 en `alarm` (INV-190, sur alarm-1 ; dossier vide
# ailleurs). 0700 : mots de passe haches et telephones. Pas de user alarm (CI) : rien.
if id alarm >/dev/null 2>&1; then
  install -d -m 700 -o alarm -g alarm /var/lib/alarm/db-backup
fi

# 2. Passerelle sur chaque machine (INV-182) : user système + accès aux ports série (dialout),
#    code gateway -> /opt/alarm-gateway/, dépendances (absentes d'une machine qui n'en a jamais eu).
if ! id alarm-gateway >/dev/null 2>&1; then
  useradd --system --no-create-home --shell /usr/sbin/nologin -G dialout alarm-gateway
  say "user alarm-gateway cree (groupe dialout)"
fi
if id -nG alarm-gateway | grep -qw dialout; then
  say "alarm-gateway déjà dans dialout"
else
  usermod -aG dialout alarm-gateway
  say "alarm-gateway ajouté à dialout (CRUCIAL — sinon Permission denied sur /dev/ttyUSB)"
fi
install -d -o alarm-gateway -g alarm-gateway -m750 /opt/alarm-gateway
# --auto : alarm-gateway-sync deploie le code depuis l'image, mais ne sait que remplacer un code
# deja la -> 1er passage : code du commit courant (celui de l'image :stable).
GW_FROM_REPO=0
if [ "$AUTO" = 1 ] && [ -f /opt/alarm-gateway/modem_gateway.py ]; then
  say "code gateway : laisse a alarm-gateway-sync (--auto)"
else
  GW_FROM_REPO=1
  for f in "$REPO"/gateway/*.py; do
    install -o alarm-gateway -g alarm-gateway -m644 "$f" "/opt/alarm-gateway/$(basename "$f")"
  done
  # Messages vocaux des appels d'alarme (INV-132/133)
  install -d -o alarm-gateway -g alarm-gateway -m755 /opt/alarm-gateway/voice
  install -o alarm-gateway -g alarm-gateway -m644 "$REPO"/gateway/voice/*.wav /opt/alarm-gateway/voice/
  say "code gateway -> /opt/alarm-gateway/"
fi
gw_deps(){ python3 -c 'import serial, requests' 2>/dev/null; }
PKGS=""
gw_deps || PKGS="python3-serial python3-requests"
command -v qmicli >/dev/null || PKGS="$PKGS libqmi-utils"  # secours 4G (alarm-4g-standby)
# alarm-gateway-sync fait `pip install -r requirements.txt` quand il change (revue PR #296, F3).
python3 -m pip --version >/dev/null 2>&1 || PKGS="$PKGS python3-pip"
apt_install(){ DEBIAN_FRONTEND=noninteractive apt-get install -y -qq -o DPkg::Lock::Timeout=120 "$@" >/dev/null; }
if [ -n "$PKGS" ]; then
  # shellcheck disable=SC2086
  if apt_install $PKGS || { apt-get update -qq >/dev/null && apt_install $PKGS; }; then
    say "paquets installes :$PKGS"
  else
    say "ATTENTION : apt-get install$PKGS en echec"
  fi
fi
# Code pose depuis le depot : ses requirements installes (meme commande qu'alarm-gateway-sync) et notes
# comme tels, sinon le 1er deploiement d'image relancerait pip pour rien (F3).
if [ "$GW_FROM_REPO" = 1 ]; then
  if python3 -m pip install --quiet --break-system-packages -r "$REPO/gateway/requirements.txt"; then
    install -D -m644 "$REPO/gateway/requirements.txt" /var/lib/alarm/cd/gateway-requirements.txt
  else
    say "ATTENTION : pip install -r gateway/requirements.txt en echec (alarm-gateway-sync le retentera)"
  fi
fi
# Un modem branche a chaud ne doit pas etre pris par ModemManager (PROVISIONING_ONSITE §2.3, F6).
if [ "$(systemctl is-enabled ModemManager 2>/dev/null)" != masked ]; then
  systemctl mask --now ModemManager >/dev/null 2>&1 || true; say "ModemManager masque"
fi

# 3. Units systemd modem + bascule box/4G (INV-140) + passerelle (versionnee depuis INV-182)
for u in alarm-4g-standby alarm-modem-rat alarm-modem-watchdog alarm-wan-failover alarm-sms-gateway; do
  inst "$REPO/infra/onsite/systemd/$u.service" "/etc/systemd/system/$u.service" 644 "$u"
done

# 3b. Supervision disque : service oneshot + timer 15 min (garde-fou anti-saturation,
#     cf incident WAL 2026-06-22). Pas de stack modem requise -> tourne aussi sur node3.
# 3c. Timers de deploiement auto (oneshot : pas de redemarrage, le prochain tick prend le neuf).
for f in alarm-disk-check.service alarm-disk-check.timer alarm-gateway-sync.service alarm-gateway-sync.timer \
         alarm-host-sync.service alarm-host-sync.timer alarm-node-check.service alarm-node-check.timer; do
  inst "$REPO/infra/onsite/systemd/$f" "/etc/systemd/system/$f" 644
done

# 4. Ancien drop-in (unit posee a la main, ExecStart redirige vers modem_gateway.py) : l'unit du
#    depot lance deja modem_gateway.py. Retire seulement s'il est tel que ce script le posait.
OVR=/etc/systemd/system/alarm-sms-gateway.service.d/override.conf
if [ -f "$OVR" ]; then
  if printf '[Service]\nExecStart=\nExecStart=python3 /opt/alarm-gateway/modem_gateway.py\n' | cmp -s - "$OVR"; then
    rm -f "$OVR"; rmdir --ignore-fail-on-non-empty "${OVR%/*}"; say "drop-in gateway d'origine retire"
  else
    say "ATTENTION : $OVR modifie a la main, conserve (verifier qu'il est encore utile)"
  fi
fi

# 5. Variables d'env gateway non-secrètes (idempotent — ne touche pas l'existant)
ENV=/etc/alarm-gateway.env
if [ ! -f "$ENV" ]; then
  install -m640 -o root -g alarm-gateway /dev/null "$ENV"
  # Contact sec par Arduino USB (INV-120 mode host, ferme = 0) : a la creation seulement, un fichier
  # existant garde sa source (revue PR #296, F2).
  printf 'DRY_CONTACT_SOURCE=host\nDRY_CONTACT_NORMAL_VALUE=0\n' >> "$ENV"
  say "$ENV cree (root:alarm-gateway 640, contact sec par Arduino)"
fi
add_env(){ grep -q "^$1=" "$ENV" || { echo "$1=$2" >> "$ENV"; say "env += $1"; }; }
add_env DRY_CONTACT_ENABLED true
add_env GATEWAY_ID "$(hostname -s)"
# 7 backends à interroger (le gateway trouve le leader via /health). IPs Wireguard
# du mesh : 10.99.0.1=node3(cloud) 10.99.0.2=node1 10.99.0.3=node2,
# 10.99.0.4=node4(alarm-1) 10.99.0.5=node5(alarm-2) 10.99.0.6=node6(alarm-3)
# 10.99.0.7=node7(alarm-4) (non votants etcd).
add_env NODE1_URL http://10.99.0.1:8000
add_env NODE2_URL http://10.99.0.2:8000
add_env NODE3_URL http://10.99.0.3:8000
add_env NODE4_URL http://10.99.0.4:8000
add_env NODE5_URL http://10.99.0.5:8000
add_env NODE6_URL http://10.99.0.6:8000
add_env NODE7_URL http://10.99.0.7:8000
grep -q "^GATEWAY_KEY=." "$ENV" || say "ATTENTION: GATEWAY_KEY manquant dans $ENV (secret, doit matcher le backend ; a poser a la main)"
grep -q "^GATEWAY_SITE=." "$ENV" || say "ATTENTION: GATEWAY_SITE manquant dans $ENV (site de CETTE machine : izeron ou beaufort ; sinon site par defaut du backend)"

# 6. Reload + enable. Pile modem et passerelle sur chaque machine (INV-182) : sans modem elles
#    attendent sans echec ni relance en boucle, et le watchdog n'agit que sur un modem deja vu
#    (INV-184). enable --now ne relance pas une unit deja active (en --auto : cf RESTART plus bas).
systemctl daemon-reload
systemctl enable alarm-modem-rat >/dev/null 2>&1 || true; say "enabled alarm-modem-rat (au boot)"
for u in alarm-4g-standby alarm-modem-watchdog; do
  systemctl enable --now "$u" >/dev/null 2>&1 || true; say "enabled+started $u"
done
if gw_deps; then
  systemctl enable --now alarm-sms-gateway >/dev/null 2>&1 || true; say "enabled+started alarm-sms-gateway"
else
  # Sans pyserial/requests elle planterait toutes les 10 s, sans fin (StartLimitIntervalSec=0).
  say "ATTENTION : pyserial/requests absents : alarm-sms-gateway NON demarree (relancer install.sh)"
fi
# Le timer disque, lui, on l'active ET on le démarre tout de suite : c'est une
# sonde read-only sans risque, et NE PAS la démarrer reproduirait la classe de bug
# de l'incident (rien ne surveillait). enable --now est idempotent.
systemctl enable --now alarm-disk-check.timer >/dev/null 2>&1 || true; say "enabled+started alarm-disk-check.timer"
# Idem : la bascule box/4G ne fait rien sans route 4G (onsite-1) et remet toujours la box
# en priorite a l'arret ; les timers de deploiement auto et de conformite doivent tourner pour exister.
for u in alarm-wan-failover alarm-gateway-sync.timer alarm-host-sync.timer alarm-node-check.timer; do
  systemctl enable --now "$u" >/dev/null 2>&1 || true; say "enabled+started $u"
done

if [ "$AUTO" = 1 ]; then
  # Units dont le script ou le fichier d'unit a change : relance (try-restart = seulement si actives).
  for u in $(echo "$RESTART" | tr ' ' '\n' | sort -u); do
    systemctl is-active -q "$u.service" && systemctl restart "$u.service" && say "redemarre $u"
  done
  say "FAIT (auto)."
  exit 0
fi

say "FAIT. À valider à la main :"
say "  GATEWAY_KEY et GATEWAY_SITE dans $ENV, puis : sudo systemctl restart alarm-sms-gateway"
say "  Pour les alertes email du watchdog : SMTP_* dans /opt/alarm/.env.prod.{node<N>,secrets}"
say "  Smoketest 4G : sudo APN=orange bash scripts/smoketest-4g.sh"
say "  Conformite au standard (INV-186, aussi chaque jour par le timer) : sudo alarm-node-check"
say "  Supervision disque (timer déjà actif) : sudo /opt/alarm/disk-check.sh --dry-run    # seuils, sans email"
say "                                          sudo /opt/alarm/disk-check.sh --test-email  # vérifie la chaîne SMTP"
say "  (mêmes SMTP_* que le watchdog — requis aussi sur node3 : /opt/alarm/.env.prod.{node3,secrets})"
