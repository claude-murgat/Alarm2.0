#!/usr/bin/env bash
# alarm-node-check [--journal] — INV-186 : conformite d'une machine sur site au standard
# (docs/STANDARD_MACHINE.md). Valeurs attendues : infra/onsite/standard.env, pose par
# infra/onsite/install.sh dans /usr/local/share/alarm/standard.env. Lecture seule, en root.
# Une ligne par controle « OK|KO|INFO <controle> <detail> » ; code 1 si au moins un KO, 2 si
# lance hors root ou sans standard.env. Machine absente du plan (STD_PLAN) : une ligne INFO, code 0.
# --journal (alarm-node-check.service) : prefixe <4> (warning) les KO, <6> (info) le reste.
# Tests : ALARM_ROOT prefixe les chemins lus (/sys, /etc, /var), commandes prises sur le PATH.
set -uo pipefail
export LC_ALL=C  # ufw, chronyc... sont traduits sinon (« État : actif »)
R="${ALARM_ROOT:-}"
STD="${ALARM_STANDARD_ENV:-/usr/local/share/alarm/standard.env}"
J=0; [ "${1:-}" = --journal ] && J=1
[ "$(id -u)" = 0 ] || { echo "alarm-node-check : a lancer en root (sudo)" >&2; exit 2; }
[ -r "$STD" ] || { echo "alarm-node-check : $STD introuvable (lancer infra/onsite/install.sh)" >&2; exit 2; }
# shellcheck source=/dev/null
. "$STD"

KO=0
out(){ local p=6; [ "$1" = KO ] && { p=4; KO=1; }; ((J)) && printf '<%s>' "$p"; echo "$1 $2 $3"; }
# chk <controle> <detail> <commande...> : OK si la commande reussit, KO sinon.
chk(){ local n=$1 d=$2; shift 2; if "$@"; then out OK "$n" "$d"; else out KO "$n" "$d"; fi; }

host=$(hostname -s)
entry=""; for e in $STD_PLAN; do [ "${e%%=*}" = "$host" ] && entry=${e#*=}; done
if [ -z "$entry" ]; then out INFO machine "$host absente du plan standard (STD_PLAN) : aucun controle"; exit 0; fi
IFS=: read -r node wg_ip site <<<"$entry"
out INFO machine "$host = $node, WireGuard $wg_ip, site $site"

# Materiel et firmware
dmi(){ cat "$R/sys/class/dmi/id/$1" 2>/dev/null; }
model=$(dmi product_name); bios=$(dmi bios_version)
chk modele "$(dmi sys_vendor) $model (attendu $STD_MODEL)" [ "$model" = "$STD_MODEL" ]
chk bios "$bios (attendu $STD_BIOS_VERSION, docs/STANDARD_MACHINE.md §2)" [ "$bios" = "$STD_BIOS_VERSION" ]
if [ -d "$R/sys/firmware/efi" ]; then out OK demarrage UEFI; else out KO demarrage "legacy (attendu UEFI : F2 > Boot List Option)"; fi
eb=$(efibootmgr -v 2>&1)
bbs=$(grep -c 'BBS(' <<<"$eb")
chk entrees_legacy "$bbs entree(s) BBS (attendu 0 : Boot List Option = UEFI, Legacy Option ROMs off)" [ "$bbs" = 0 ]
sb=$(od -An -tu1 -j4 -N1 "$R/sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c" 2>/dev/null | tr -d ' ')
case "$sb" in 1) sb=enabled ;; 0) sb=disabled ;; *) sb=inconnu ;; esac
chk secure_boot "$sb (attendu $STD_SECURE_BOOT)" [ "$sb" = "$STD_SECURE_BOOT" ]
first=$(sed -n 's/^BootOrder: *\([0-9A-Fa-f]\{4\}\).*/\1/p' <<<"$eb")
label=$(grep -m1 "^Boot${first:-none}" <<<"$eb" | cut -f1)
chk premier_demarrage "${label:-aucun BootOrder} (attendu debian)" grep -qiE "^Boot${first:-none}\*? debian" <<<"$eb"

# Systeme
osv=$(. "$R/etc/os-release" 2>/dev/null; echo "${ID:-?} ${VERSION_ID:-?}")
chk os "$osv (attendu $STD_OS_ID $STD_OS_VERSION_ID)" [ "$osv" = "$STD_OS_ID $STD_OS_VERSION_ID" ]
a=$(dpkg --audit 2>&1)
chk dpkg_audit "${a:+paquets inacheves : $(echo $a | cut -c1-80)}${a:-propre}" [ -z "$a" ]
ns=" $(awk '$1 == "nameserver" {printf "%s ", $2}' "$R/etc/resolv.conf" 2>/dev/null)"
pub=0; for p in $STD_DNS_PUBLIC; do [[ "$ns" == *" $p "* ]] && pub=1; done
chk dns_secours "resolveurs${ns% } (attendu un de $STD_DNS_PUBLIC, INV-181)" [ "$pub" = 1 ]
u=$(ufw status 2>&1 | head -1)
chk ufw "$u" [ "$u" = "Status: active" ]
leap=$(chronyc -n tracking 2>/dev/null | sed -n 's/^Leap status *: *//p')
chk chrony "Leap status ${leap:-inconnu}" [ "$leap" = Normal ]
w=$(systemctl show -p RuntimeWatchdogUSec --value 2>/dev/null)
chk watchdog "RuntimeWatchdogUSec=${w:-?} (attendu $STD_RUNTIME_WATCHDOG)" [ "$w" = "$STD_RUNTIME_WATCHDOG" ]
chk journald "/var/log/journal (journal persistant)" [ -d "$R/var/log/journal" ]
mm=$(systemctl is-enabled ModemManager 2>/dev/null)
chk modemmanager "${mm:-absent} (attendu masked)" [ "$mm" = masked ]
d=$(systemctl is-active docker 2>/dev/null)
chk docker "docker ${d:-absent}" [ "$d" = active ]

# WireGuard : adresse du plan, NODE3 (relais) vu recemment
addr=$(ip -4 -o addr show dev "$STD_WG_IFACE" 2>/dev/null | awk '{print $4; exit}')
chk wg0 "${addr:-interface absente} (attendu $wg_ip/24)" [ "$addr" = "$wg_ip/24" ]
ts=$(wg show "$STD_WG_IFACE" latest-handshakes 2>/dev/null | awk -v k="$STD_NODE3_PUBKEY" '$1 == k {print $2}')
[[ "$ts" =~ ^[0-9]+$ ]] || ts=0
age=$(( $(date +%s) - ts )); fresh=0
(( ts > 0 && age <= STD_WG_HANDSHAKE_MAX_AGE )) && fresh=1
if (( ts > 0 )); then hd="NODE3 il y a $age s"; else hd="aucune poignee de main avec NODE3"; fi
chk wg0_node3 "$hd (max $STD_WG_HANDSHAKE_MAX_AGE s)" [ "$fresh" = 1 ]

# Passerelle (INV-182) : la cle n'est jamais affichee
gv(){ sed -n "s/^$1=//p" "$R/etc/alarm-gateway.env" 2>/dev/null | tail -1 | tr -d '"'; }
case "$(gv GATEWAY_KEY)" in "") kd=absente ;; changeme-in-prod) kd="valeur par defaut" ;; *) kd=definie ;; esac
chk passerelle_cle "GATEWAY_KEY $kd" [ "$kd" = definie ]
s=$(gv GATEWAY_SITE); chk passerelle_site "GATEWAY_SITE=${s:-absent} (attendu $site)" [ "$s" = "$site" ]
c=$(gv DRY_CONTACT_SOURCE); chk passerelle_contact_sec "DRY_CONTACT_SOURCE=${c:-absent} (attendu host)" [ "$c" = host ]

# Patroni (INV-188) : configuration dynamique (etcd), lue sur l'API du membre local. Le port 8008 est
# lie a ADVERTISE_HOST (IP WireGuard), pas a 127.0.0.1. Un archive_timeout sans vrai archivage force
# un segment WAL de 16 Mo toutes les N s (incident du 2026-11-06 : 6 Go/h vers les replicas).
pc=$(curl -fsS --max-time 5 "http://$wg_ip:8008/config" 2>/dev/null | python3 -c '
import json, sys
p = json.load(sys.stdin).get("postgresql", {}).get("parameters", {})
print(p.get("archive_timeout") or 0); print(p.get("archive_command") or "")' 2>/dev/null)
if [ -z "$pc" ]; then
  out INFO patroni_archive "API Patroni http://$wg_ip:8008/config injoignable ou illisible : non controle"
else
  { read -r at; IFS= read -r ac; } <<<"$pc"
  forced=1; [[ "$at" =~ ^0+[a-z]*$ ]] && forced=0  # 0, 0s, 0min : aucune bascule forcee
  case "$ac" in ""|true|/bin/true|/usr/bin/true|:) real=0 ;; *) real=1 ;; esac
  chk patroni_archive "archive_timeout=$at archive_command='$ac' (attendu archive_timeout=0 sans vrai archivage : patronictl edit-config -p archive_timeout=0)" \
    [ "$forced$real" != 10 ]
fi

# Copie des sauvegardes de la base (INV-190), sur la machine qui la recoit de NODE3 : temoin independant
# de NODE3 (arrete, timer desactive : aucun email). Date de reception = mtime (renommee une fois complete).
if [ "$node" = "${STD_DB_BACKUP_COPY_NODE:-}" ]; then
  bt=$(find "$R$STD_DB_BACKUP_DIR" -maxdepth 1 -name 'alarm_db-*.dump' -printf '%T@\n' 2>/dev/null | sort -n | tail -1)
  bt=${bt%.*}; [[ "$bt" =~ ^[0-9]+$ ]] || bt=0
  bage=$(( $(date +%s) - bt )); fresh=0
  (( bt > 0 && bage <= STD_DB_BACKUP_MAX_AGE_HOURS * 3600 )) && fresh=1
  if (( bt > 0 )); then bd="copie la plus recente recue il y a $((bage / 3600)) h"; else bd="aucune copie dans $STD_DB_BACKUP_DIR"; fi
  chk sauvegarde_base "$bd (max $STD_DB_BACKUP_MAX_AGE_HOURS h, INV-190)" [ "$fresh" = 1 ]
fi

for unit in $STD_UNITS; do
  e=$(systemctl is-enabled "$unit" 2>/dev/null); chk "unit:$unit" "${e:-absente}" [ "$e" = enabled ]
done
exit "$KO"
