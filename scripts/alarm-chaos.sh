#!/bin/bash
# alarm-chaos.sh — panne controlee (INV-176..179) : execute les demandes de l'onglet
# Tests (POST /api/chaos) : arrete les conteneurs d'un noeud pendant T min, puis les
# relance. Mode « stop » seulement ; « netcut » arrive avec la PR 5 (ici -> failed).
#
# Tourne via alarm-chaos.timer (1 min) sur NODE3 cloud — le seul noeud qui voit
# les 5 cibles via le mesh WG, comme alarm-cd-orchestrate. Livre desactive et
# installe a la main (cf docs/CD_DESIGN.md « Panne controlee »).
#
# Garantie INV-178 : la restauration est ARMEE SUR LA CIBLE avant l'arret
# (setsid nohup sh -c 'sleep T+60 ; docker start ...', detache de la session SSH) :
# le noeud redemarre meme si ce runner, NODE3 ou le reseau disparaissent.
#
# Verrou partage avec l'orchestrateur CD (${STATE_DIR}/orchestrate.lock) : pas de
# deploiement pendant un test, pas de test pendant un deploiement. Verrou pris ->
# on ne reclame rien (la demande reste pending pour le tick suivant).
#
# Sortie : 0 rien a faire / verrou pris / test restaure ; 1 config KO ou test en echec
# (l'echec est deja envoye au backend, event « failed »).

set -euo pipefail

STATE_DIR="${STATE_DIR:-/var/lib/alarm/cd}"
LOCK_FILE="${LOCK_FILE:-${STATE_DIR}/orchestrate.lock}"
# shortcut: MINUTE_SECONDS=1 dans tests/unit/test_alarm_chaos.py (une minute de panne = une seconde) ; ne jamais le regler en prod.
MINUTE_SECONDS="${MINUTE_SECONDS:-60}"
HEALTHY_TIMEOUT="${HEALTHY_TIMEOUT:-300}"
NODE_IPS="10.99.0.1 10.99.0.2 10.99.0.3 10.99.0.4 10.99.0.5"
# IP WG par nom de projet compose (cf alarm-cd-orchestrate.sh NODE_IP, indexe par numero).
declare -A NODE_IP=([node1]=10.99.0.2 [node2]=10.99.0.3 [node3]=10.99.0.1 [node4]=10.99.0.4 [node5]=10.99.0.5)

log() { echo "[alarm-chaos] $*" >&2; }
py() { python3 -c "import sys, json; d = json.load(sys.stdin); $1"; }

if [[ -z "${GATEWAY_KEY:-}" ]]; then
  log "ERREUR : GATEWAY_KEY non definie. Creer /etc/alarm/cd.env avec GATEWAY_KEY=..."
  exit 1
fi

mkdir -p "$STATE_DIR"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "Verrou $LOCK_FILE pris (deploiement CD en cours) : aucun test ce tick"
  exit 0
fi

# Leader Patroni = seul noeud qui accepte les ecritures /internal/* (replicas -> 503).
discover_leader() {
  local ip role
  for ip in $NODE_IPS; do
    role=$(curl -fsS --max-time 3 "http://${ip}:8000/health" 2>/dev/null | py "print(d.get('role', ''))" 2>/dev/null || true)
    if [[ "$role" == "primary" ]]; then echo "$ip"; return 0; fi
  done
  return 1
}

# /api/cluster du premier noeud joignable (le noeud arrete ne repond plus).
cluster_json() {
  local ip
  for ip in $NODE_IPS; do
    curl -fsS --max-time 3 "http://${ip}:8000/api/cluster" 2>/dev/null && return 0
  done
  return 1
}
cluster_leader() {
  cluster_json | py "print(next((m['name'] for m in d.get('members', []) if m.get('role') == 'leader'), ''))" 2>/dev/null || true
}
member_state() {  # $1 = noeud
  cluster_json | py "print(next((m.get('state', '') for m in d.get('members', []) if m.get('name') == '$1'), ''))" 2>/dev/null || true
}

API_BASE=""
api_call() {  # $1 chemin, $2 body JSON (POST) ou rien (GET). Redecouvre le leader si l'appel echoue : il peut etre la cible du test.
  local try
  for try in 1 2; do
    [[ -n "$API_BASE" ]] || API_BASE="http://$(discover_leader):8000" || { API_BASE=""; continue; }
    if [[ -n "${2:-}" ]]; then
      curl -fsS --max-time 5 -X POST -H "X-Gateway-Key: $GATEWAY_KEY" -H "Content-Type: application/json" \
        -d "$2" "${API_BASE}$1" && return 0
    else
      curl -fsS --max-time 5 -H "X-Gateway-Key: $GATEWAY_KEY" "${API_BASE}$1" && return 0
    fi
    API_BASE=""
  done
  return 1
}

NEXT=$(api_call /internal/chaos/next) || { log "WARN : aucun leader joignable, GET /internal/chaos/next KO"; exit 0; }
if [[ -z "$NEXT" || "$NEXT" == "null" ]]; then
  exit 0  # rien en attente : silencieux (un tick par minute)
fi
read -r ID MODE T NODES < <(printf '%s' "$NEXT" | py "print(d['id'], d['mode'], d['minutes'], ','.join(d['nodes']))")
log "Panne controlee #$ID : $MODE $NODES pendant $T min"

post_event() {  # $1 kind, $2 details JSON
  api_call "/internal/chaos/${ID}/events" "{\"kind\":\"$1\",\"details\":$2}" >/dev/null || log "WARN : POST $1 KO"
}
ssh_node() {  # $1 noeud, $2 commande distante
  ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new "alarm@${NODE_IP[$1]}" "$2"
}

STOPPED=()
declare -A CIDS_OF
fail() {
  log "ECHEC : $1"
  local n
  for n in "${STOPPED[@]}"; do  # relance tout de suite ce qui est deja arrete (la restauration armee reste en filet)
    ssh_node "$n" "docker start ${CIDS_OF[$n]}" || log "WARN : docker start $n KO (restauration armee sur le noeud dans $((T * MINUTE_SECONDS + 60)) s)"
  done
  post_event failed "{\"reason\":$(printf '%s' "$1" | python3 -c 'import sys, json; print(json.dumps(sys.stdin.read()))')}"
  exit 1
}

LEADER_BEFORE=$(cluster_leader)
post_event started "{\"leader_before\":\"$LEADER_BEFORE\"}"
if [[ "$MODE" != "stop" ]]; then fail "mode $MODE non implemente par ce runner (PR 5)"; fi

for n in ${NODES//,/ }; do
  [[ -n "${NODE_IP[$n]:-}" ]] || fail "noeud inconnu du runner : $n"
  cids=$(ssh_node "$n" "docker ps -q --filter label=com.docker.compose.project=$n" | xargs) || fail "ssh / docker ps KO sur $n"
  [[ -n "$cids" ]] || fail "aucun conteneur en cours sur $n"
  CIDS_OF[$n]=$cids
  # INV-178 : armer la restauration SUR LA CIBLE avant d'agir, detachee de la session SSH.
  ssh_node "$n" "setsid nohup sh -c 'sleep $((T * MINUTE_SECONDS + 60)); docker start $cids' >/dev/null 2>&1 </dev/null &" \
    || fail "armement de la restauration KO sur $n : rien n'est arrete"
  ssh_node "$n" "docker stop $cids" || fail "docker stop KO sur $n"
  STOPPED+=("$n")
  log "$n arrete : $cids"
done

# Pendant la panne : suivi du leader Patroni toutes les 10 s.
POLL=$(( MINUTE_SECONDS < 10 ? MINUTE_SECONDS : 10 ))
LEADER=$LEADER_BEFORE
for (( elapsed = 0; elapsed < T * MINUTE_SECONDS; elapsed += POLL )); do
  sleep "$POLL"
  now_leader=$(cluster_leader)
  if [[ -n "$now_leader" && "$now_leader" != "$LEADER" ]]; then
    log "leader Patroni : ${LEADER:-aucun} -> $now_leader"
    post_event leader_changed "{\"from\":\"$LEADER\",\"to\":\"$now_leader\"}"
    LEADER=$now_leader
  fi
done

wait_healthy() {  # $1 noeud : /health 200 ET membre Patroni running|streaming, en <= HEALTHY_TIMEOUT s
  local deadline=$(( $(date +%s) + HEALTHY_TIMEOUT )) state
  while (( $(date +%s) < deadline )); do
    state=$(member_state "$1")
    if curl -fsS --max-time 3 "http://${NODE_IP[$1]}:8000/health" >/dev/null 2>&1 \
       && [[ "$state" == "running" || "$state" == "streaming" ]]; then
      return 0
    fi
    sleep 5
  done
  return 1
}

T0=$(date +%s)
for n in "${STOPPED[@]}"; do
  ssh_node "$n" "docker start ${CIDS_OF[$n]}" || fail "docker start KO sur $n (restauration armee sur le noeud en filet)"
done
for n in "${STOPPED[@]}"; do
  wait_healthy "$n" || fail "$n pas sain ${HEALTHY_TIMEOUT} s apres docker start"
done
RESTORE_SECONDS=$(( $(date +%s) - T0 ))
post_event restored "{\"leader_after\":\"$(cluster_leader)\",\"restore_seconds\":$RESTORE_SECONDS}"
log "Panne controlee #$ID terminee : $NODES restaure(s) en $RESTORE_SECONDS s"
exit 0
