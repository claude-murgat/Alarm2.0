#!/usr/bin/env bash
# /opt/alarm/4g-standby.sh — lien data 4G (SIM7600/QMI) en standby chaud, metrique 300
# (non-disruptif : ne porte du trafic que si fibre+Starlink tombent). Reconnecte si la session tombe.
set -uo pipefail
WDM="${WDM:-/dev/cdc-wdm0}"; APN="${APN:-orange}"; METRIC="${METRIC:-300}"; CHECK="${CHECK:-30}"
log(){ echo "$(date '+%F %T') [4g-standby] $*"; }
for i in $(seq 1 30); do [ -e "$WDM" ] && break; sleep 2; done
[ -e "$WDM" ] || { log "modem $WDM absent apres 60s — abandon (systemd relancera)"; exit 1; }
IF=""
for c in /sys/class/net/ww*; do [ -e "$c/device/driver" ] || continue; case "$(readlink -f "$c/device/driver")" in *qmi_wwan*) IF="$(basename "$c")"; break;; esac; done
[ -n "$IF" ] || { log "interface qmi_wwan introuvable"; exit 1; }
HANDLE=""; CID=""; GW4=""; IP4=""
teardown(){
  log "stop : demontage du lien 4G"
  [ -n "$HANDLE" ] && qmicli -d "$WDM" -p --wds-stop-network="$HANDLE" --client-cid="${CID:-}" >/dev/null 2>&1
  ip route del default dev "$IF" metric "$METRIC" 2>/dev/null
  ip addr flush dev "$IF" 2>/dev/null; ip link set "$IF" down 2>/dev/null
  exit 0
}
trap teardown TERM INT
bring_up(){
  qmicli -d "$WDM" -p --wds-stop-network=disable >/dev/null 2>&1 || true
  ip link set "$IF" down 2>/dev/null
  echo Y > "/sys/class/net/$IF/qmi/raw_ip" 2>/dev/null || true
  ip link set "$IF" up
  local start; start="$(qmicli -d "$WDM" -p --wds-start-network="apn='$APN',ip-type=4" --client-no-release-cid 2>&1)"
  HANDLE="$(grep -oE "handle: '[0-9]+'" <<<"$start" | grep -oE '[0-9]+' | head -1)"
  CID="$(grep -oE "CID: '[0-9]+'" <<<"$start" | grep -oE '[0-9]+' | head -1)"
  [ -n "$HANDLE" ] || { log "echec WDS start : $(tail -1 <<<"$start")"; return 1; }
  local s; s="$(qmicli -d "$WDM" -p --wds-get-current-settings 2>&1)"
  IP4="$(awk -F': ' '/IPv4 address/{print $2}' <<<"$s" | tr -d ' ')"
  GW4="$(awk -F': ' '/IPv4 gateway address/{print $2}' <<<"$s" | tr -d ' ')"
  [ -n "$IP4" ] && [ -n "$GW4" ] || { log "pas d'IP attribuee"; return 1; }
  ip addr replace "$IP4/30" dev "$IF"
  ip route replace default via "$GW4" dev "$IF" metric "$METRIC"
  log "lien 4G UP : $IP4 via $GW4 (metrique $METRIC, standby chaud, fibre prioritaire)"
}
connected(){ qmicli -d "$WDM" -p --wds-get-packet-service-status 2>/dev/null | grep -qi "'connected'"; }
until bring_up; do log "retry activation dans 15s"; sleep 15; done
while true; do
  sleep "$CHECK"
  if ! connected; then
    log "session data tombee -> reconnexion"; HANDLE=""; CID=""
    until bring_up; do log "retry dans 15s"; sleep 15; done
  elif [ -n "$GW4" ] && ! ip route show default | grep -q "dev $IF metric $METRIC"; then
    log "route 4G manquante -> reajout"; ip route replace default via "$GW4" dev "$IF" metric "$METRIC" 2>/dev/null || true
  fi
done
