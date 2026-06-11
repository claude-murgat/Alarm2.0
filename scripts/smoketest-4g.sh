#!/usr/bin/env bash
# smoketest-4g.sh — Smoketest du failover internet 4G (SIM7600E-H) sur un nœud on-site.
#
# Matériel réel (onsite-2) : le module énumère en QMI (qmi_wwan / /dev/cdc-wdm0), PAS en ECM.
# Ce script monte donc le lien data via libqmi (qmicli, raw-ip) — control sur cdc-wdm0, ce qui
# laisse les ports AT (/dev/ttyUSBx) à la gateway SMS. Runbook : docs/FAILOVER_4G.md.
#
# Usage :
#   sudo bash scripts/smoketest-4g.sh                 # L1 : monte la 4G, teste, démonte (non-disruptif)
#   sudo bash scripts/smoketest-4g.sh --keep-up       # L1 puis laisse la 4G en standby (métrique 300)
#   sudo bash scripts/smoketest-4g.sh --failover      # L1 + L2 : bascule réelle (auto-revert 180s)
#   APN=orange IF=wwp0s20f0u4i5 sudo -E bash scripts/smoketest-4g.sh   # Sosh/Orange ; 'free' pour Free Mobile
#
# Exit code 0 = PASS, !=0 = FAIL (utilisable comme gate CI).
set -uo pipefail

APN="${APN:-orange}"   # SIM réelle onsite-2 = Sosh (MVNO Orange) → APN 'orange'. Free Mobile = 'free'.
WDM="${WDM:-/dev/cdc-wdm0}"
NODE3="${NODE3:-51.210.105.102}"   # IP publique OVH (mesh WG)
NODE3_WG="${NODE3_WG:-10.99.0.1}"  # IP Wireguard NODE3
KEEP_UP=0; DO_FAILOVER=0
for a in "$@"; do case "$a" in
  --keep-up) KEEP_UP=1 ;;
  --failover) DO_FAILOVER=1 ;;
  *) echo "arg inconnu: $a" >&2; exit 2 ;;
esac; done

PASS=0; FAIL=0
ok()   { echo "  [PASS] $*"; PASS=$((PASS+1)); }
ko()   { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
info() { echo "  - $*"; }
hdr()  { echo; echo "== $* =="; }

[ "$(id -u)" -eq 0 ] || { echo "Ce script doit tourner en root (sudo)." >&2; exit 2; }

# --- Découverte de l'interface 4G (qmi_wwan) ---------------------------------
IF="${IF:-}"
if [ -z "$IF" ]; then
  for c in /sys/class/net/ww*; do
    [ -e "$c/device/driver" ] || continue
    case "$(readlink -f "$c/device/driver")" in *qmi_wwan*) IF="$(basename "$c")"; break;; esac
  done
fi
[ -n "$IF" ] || { echo "Aucune interface qmi_wwan trouvée." >&2; exit 2; }

# --- Baseline route fibre (pour assertions + auto-revert L2) -----------------
read -r FIBER_GW FIBER_DEV < <(ip -4 route show default | awk '/default/{for(i=1;i<=NF;i++){if($i=="via")g=$(i+1);if($i=="dev")d=$(i+1)}print g,d; exit}')
[ -n "${FIBER_GW:-}" ] && [ -n "${FIBER_DEV:-}" ] || { echo "Pas de route par défaut fibre détectée — abandon." >&2; exit 2; }

HANDLE=""; CID=""
cleanup() {
  hdr "Cleanup"
  if [ "$KEEP_UP" -eq 1 ]; then
    info "4G laissée ARMÉE : session WDS active + route défaut métrique 300 (standby chaud)"
  else
    [ -n "$HANDLE" ] && qmicli -d "$WDM" -p --wds-stop-network="$HANDLE" --client-cid="${CID:-}" >/dev/null 2>&1 && info "WDS stoppé"
    ip route del default dev "$IF" 2>/dev/null && info "route 4G retirée" || true
    ip addr flush dev "$IF" 2>/dev/null || true
    ip link set "$IF" down 2>/dev/null && info "interface remise DOWN" || true
  fi
  # garde-fou : la route par défaut fibre doit être intacte
  ip route replace default via "$FIBER_GW" dev "$FIBER_DEV" onlink 2>/dev/null || true
}
trap cleanup EXIT

mask2cidr() { local m=$1 b=0 o; for o in ${m//./ }; do case $o in 255)b=$((b+8));;254)b=$((b+7));;252)b=$((b+6));;248)b=$((b+5));;240)b=$((b+4));;224)b=$((b+3));;192)b=$((b+2));;128)b=$((b+1));;0);;*)echo 32;return;;esac; done; echo $b; }

# --- Prérequis : libqmi-utils -----------------------------------------------
hdr "Prérequis"
info "nœud : $(hostname)   interface 4G : $IF   fibre : $FIBER_GW dev $FIBER_DEV"
if ! command -v qmicli >/dev/null; then
  info "libqmi-utils absent → installation"
  apt-get update -qq && apt-get install -y -qq libqmi-utils || { echo "échec install libqmi-utils" >&2; exit 1; }
fi
command -v qmicli >/dev/null && ok "qmicli dispo" || { ko "qmicli indispo"; exit 1; }
[ -e "$WDM" ] && ok "control QMI $WDM présent" || { ko "$WDM absent"; exit 1; }

# --- Activation QMI (raw-ip) -------------------------------------------------
hdr "Activation lien data (QMI raw-ip, APN=$APN)"
qmicli -d "$WDM" -p --wds-stop-network=disable >/dev/null 2>&1 || true   # best-effort pre-clean
ip link set "$IF" down 2>/dev/null || true
echo 'Y' > "/sys/class/net/$IF/qmi/raw_ip" 2>/dev/null && info "raw_ip activé" || info "raw_ip déjà actif"
ip link set "$IF" up

START="$(qmicli -d "$WDM" -p --wds-start-network="apn='$APN',ip-type=4" --client-no-release-cid 2>&1)"
HANDLE="$(grep -oE "handle: '[0-9]+'" <<<"$START" | grep -oE '[0-9]+' | head -1)"
CID="$(grep -oE "CID: '[0-9]+'" <<<"$START" | grep -oE '[0-9]+' | head -1)"
[ -n "$HANDLE" ] && ok "WDS démarré (handle=$HANDLE cid=$CID)" || { ko "WDS start échoué"; echo "$START"; exit 1; }

SET="$(qmicli -d "$WDM" -p --wds-get-current-settings 2>&1)"
IP4="$(awk -F': ' '/IPv4 address/{print $2}' <<<"$SET" | tr -d ' ')"
GW4="$(awk -F': ' '/IPv4 gateway address/{print $2}' <<<"$SET" | tr -d ' ')"
MK4="$(awk -F': ' '/IPv4 subnet mask/{print $2}' <<<"$SET" | tr -d ' ')"
[ -n "$IP4" ] && [ -n "$GW4" ] || { ko "pas d'IP attribuée par le réseau"; echo "$SET"; exit 1; }
PFX="$(mask2cidr "${MK4:-255.255.255.248}")"
ip addr replace "$IP4/$PFX" dev "$IF"
ip route replace default via "$GW4" dev "$IF" metric 300   # métrique 300 = sous la fibre (0), non-prioritaire
ok "IP 4G $IP4/$PFX via $GW4 (route défaut métrique 300)"

# ===========================================================================
hdr "SMOKE L1 — accès internet via 4G, sans perturber le routage de prod"
# (a) l'IP publique vue par la 4G doit différer de l'egress fibre
FIB_PUB="$(curl -4 -s --max-time 8 --interface "$FIBER_DEV" https://api.ipify.org || true)"
CAR_PUB="$(curl -4 -s --max-time 12 --interface "$IF" https://api.ipify.org || true)"
info "egress fibre = ${FIB_PUB:-?}   egress 4G = ${CAR_PUB:-?}"
if [ -n "$CAR_PUB" ] && [ "$CAR_PUB" != "$FIB_PUB" ]; then ok "internet via 4G OK, IP opérateur distincte"; else ko "pas d'IP opérateur 4G distincte"; fi
# (b) latence brute 4G
ping -I "$IF" -c3 -W3 1.1.1.1 >/dev/null 2>&1 && ok "ping 1.1.1.1 via 4G" || ko "ping 1.1.1.1 via 4G KO"
# (c) NODE3 cloud joignable par la 4G
ping -I "$IF" -c3 -W3 "$NODE3" >/dev/null 2>&1 && ok "ping NODE3 ($NODE3) via 4G" || ko "ping NODE3 via 4G KO"
# (d) le routage de prod n'a PAS bougé : trafic non-bindé sort toujours par la fibre
if ip route get 1.1.1.1 2>/dev/null | grep -q "dev $FIBER_DEV"; then ok "route par défaut intacte (fibre $FIBER_DEV)"; else ko "ROUTE DÉFAUT MODIFIÉE — anormal"; fi
# (e) compteur data consommée
RX="$(cat "/sys/class/net/$IF/statistics/rx_bytes")"; TX="$(cat "/sys/class/net/$IF/statistics/tx_bytes")"
info "data 4G consommée : rx=$RX o, tx=$TX o"

# ===========================================================================
if [ "$DO_FAILOVER" -eq 1 ]; then
  hdr "SMOKE L2 — bascule réelle (démontage fibre, auto-revert 180s)"
  echo "  !! la sortie internet passe sur 4G pendant ~le test. SSH LAN non affecté."
  # filet : restaure la fibre quoi qu'il arrive dans 180s
  systemd-run --on-active=180 --timer-property=AccuracySec=1s \
    ip route replace default via "$FIBER_GW" dev "$FIBER_DEV" onlink >/dev/null 2>&1 \
    && info "auto-revert programmé (180s)" || info "systemd-run indispo — revert manuel uniquement"
  ip route del default via "$FIBER_GW" dev "$FIBER_DEV" metric 9000 2>/dev/null   # nettoie un résidu éventuel
  ip route del default via "$FIBER_GW" dev "$FIBER_DEV" 2>/dev/null                # RETIRE la fibre → 4G (métrique 300) devient le défaut
  sleep 2
  ip route get "$NODE3" 2>/dev/null | grep -q "dev $IF" && ok "egress NODE3 bascule sur 4G" || ko "pas de bascule egress"
  wg show wg0 latest-handshakes 2>/dev/null | grep -q . && ok "WG wg0 a des handshakes" || info "WG : pas d'info (wg absent ?)"
  ping -c3 -W4 "$NODE3_WG" >/dev/null 2>&1 && ok "ping NODE3 WG ($NODE3_WG) via tunnel sur 4G" || ko "NODE3 WG injoignable sur 4G"
  curl -s --max-time 10 -o /dev/null -w '%{http_code}' "http://$NODE3_WG:8000/health" 2>/dev/null | grep -q 200 && ok "/health cloud OK via 4G" || info "/health cloud : non vérifié"
  if command -v patronictl >/dev/null && [ -f /opt/alarm/patroni/patroni.yml ]; then
    n="$(patronictl -c /opt/alarm/patroni/patroni.yml list 2>/dev/null | grep -cE 'Leader|Replica' || echo 0)"
    [ "$n" -ge 2 ] && ok "Patroni voit $n membres" || info "Patroni : $n membres visibles"
  else info "patronictl/conf absent — check cluster sauté"; fi
  curl -s --max-time 10 -o /dev/null "https://fcm.googleapis.com" && ok "FCM googleapis joignable via 4G" || ko "FCM injoignable via 4G"
  # restore immédiat (sans attendre le timer)
  ip route replace default via "$FIBER_GW" dev "$FIBER_DEV" onlink
  ip route get 1.1.1.1 2>/dev/null | grep -q "dev $FIBER_DEV" && ok "fibre restaurée comme défaut" || ko "restauration fibre KO"
fi

# ===========================================================================
hdr "Bilan"
echo "  PASS=$PASS  FAIL=$FAIL"
if [ "$FAIL" -eq 0 ]; then echo "  >>> SMOKE 4G : PASS"; exit 0; else echo "  >>> SMOKE 4G : FAIL"; exit 1; fi
