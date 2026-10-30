#!/bin/bash
# alarm-chaos.sh — panne controlee (INV-176..179) : execute les demandes de l'onglet
# Tests (POST /api/chaos) pendant T min, puis restaure :
#   stop   : arrete les conteneurs du noeud (docker stop), les relance a T ;
#   netcut : coupe le tunnel wg0 du noeud via le helper root de la cible
#            (sudo -n alarm-chaos-net cut T*60), qui le remet lui-meme a T.
#
# Tourne via alarm-chaos.timer (1 min) sur NODE3 cloud — le seul noeud qui voit
# les 5 cibles via le mesh WG, comme alarm-cd-orchestrate. Livre desactive et
# installe a la main (cf docs/CD_DESIGN.md « Panne controlee »). D'ou netcut
# refuse sur node3 (INV-177) : couper OVH rendrait ce runner aveugle.
#
# Garantie INV-178 : la restauration est ARMEE SUR LA CIBLE avant d'agir.
#   stop   : `docker update --restart=always` (un reboot de l'hote relance les conteneurs) et
#            setsid nohup sh -c 'sleep ... ; docker start ...', detachee de la session SSH,
#            prouvee par ARMED puis pgrep ;
#   netcut : timer systemd root alarm-chaos-restore pose par le helper.
# Le noeud revient meme si ce runner, NODE3 ou le reseau disparaissent.
# INV-177 : etcd de chaque voteur sonde juste avant d'agir.
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
member_ok() {  # $1 = noeud : leader `running`, replica `streaming` (un replica `running` est bloque, INV-168)
  [[ "$(cluster_json | py "m = next((m for m in d.get('members', []) if m.get('name') == '$1'), {}); print(m.get('state') == ('running' if m.get('role') == 'leader' else 'streaming'))" 2>/dev/null)" == "True" ]]
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
read -r ID MODE T NODES VOTERS < <(printf '%s' "$NEXT" | py "print(d['id'], d['mode'], d['minutes'], ','.join(d['nodes']), ','.join(d.get('voters', [])))")
log "Panne controlee #$ID : $MODE $NODES pendant $T min"

post_event() {  # $1 kind, $2 details JSON
  api_call "/internal/chaos/${ID}/events" "{\"kind\":\"$1\",\"details\":$2}" >/dev/null || log "WARN : POST $1 KO"
}
ssh_node() {  # $1 noeud, $2 commande distante. ServerAlive : un trou reseau en pleine commande echoue en ~15 s.
  ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=3 \
    -o StrictHostKeyChecking=accept-new "alarm@${NODE_IP[$1]}" "$2"
}
restore_node() {  # $1 noeud : relance ses conteneurs, remet la politique de compose, puis desarme le filet
  # de CE test (sinon il relancerait le noeud a l'echeance + 60 s, en plein test suivant) ; motif ancre : l'id 7 ne tue pas l'id 70.
  ssh_node "$1" "docker start ${CIDS_OF[$1]} && docker update --restart=unless-stopped ${CIDS_OF[$1]} >/dev/null && { pkill -f '[a]larm-chaos-$ID\$' || true; }"
}
json_str() { printf '%s' "$1" | python3 -c 'import sys, json; print(json.dumps(sys.stdin.read()))'; }
abort_reason() {  # raison de restaurer tout de suite (annulation admin, alarme survenue), vide sinon
  api_call "/internal/chaos/${ID}/abort" | py "print(d.get('abort') or '')" 2>/dev/null || true
}

STOPPED=()
declare -A CIDS_OF
restore_now() {  # $1 noeud : relance / reconnecte tout de suite (la restauration armee sur la cible reste en filet)
  if [[ "$MODE" == netcut ]]; then
    ssh_node "$1" "sudo -n /usr/local/bin/alarm-chaos-net restore"
  else
    restore_node "$1"
  fi
}
fail() {
  log "ECHEC : $1"
  local n
  # stop : tout noeud liste, meme arrete a moitie ou seulement passe en restart=always (docker start est
  # sans effet sur un conteneur en marche) ; netcut : les noeuds coupes. La restauration armee reste en filet.
  local touched=("${!CIDS_OF[@]}")
  [[ "$MODE" == netcut ]] && touched=("${STOPPED[@]}")
  for n in "${touched[@]}"; do
    restore_now "$n" || log "WARN : restauration immediate de $n KO (restauration armee sur le noeud en filet)"
  done
  post_event failed "{\"reason\":$(json_str "$1")}"
  exit 1
}

LEADER_BEFORE=$(cluster_leader)
post_event started "{\"leader_before\":\"$LEADER_BEFORE\"}"
if [[ "$MODE" != "stop" && "$MODE" != "netcut" ]]; then fail "mode $MODE inconnu de ce runner"; fi
# INV-177 : le backend refuse deja netcut node3 ; ce runner vit sur node3, il refuse aussi.
if [[ "$MODE" == netcut && ",$NODES," == *,node3,* ]]; then fail "coupure reseau de node3 refusee : ce runner y vit (INV-177)"; fi

# INV-177 : etat REEL d'etcd juste avant d'agir (un etcd deja tombe + un voteur arrete = quorum perdu).
[[ -n "$VOTERS" ]] || fail "voteurs etcd absents de /internal/chaos/next : rien n'est arrete"
for v in ${VOTERS//,/ }; do
  [[ -n "${NODE_IP[$v]:-}" ]] || fail "voteur etcd inconnu du runner : $v, rien n'est arrete"
  curl -sf --max-time 3 "http://${NODE_IP[$v]}:2379/health" >/dev/null \
    || fail "etcd de $v injoignable ou pas sain : quorum non garanti, rien n'est arrete"
done

# Echeance absolue : la fenetre finit a DEADLINE quelle que soit la duree des appels ; les filets tirent a DEADLINE + 60 s.
DEADLINE=$(( $(date +%s) + T * MINUTE_SECONDS ))
for n in ${NODES//,/ }; do
  [[ -n "${NODE_IP[$n]:-}" ]] || fail "noeud inconnu du runner : $n"
  if [[ "$MODE" == netcut ]]; then
    # INV-178 : le helper root arme sur la cible le timer de restauration et le prouve actif AVANT la coupure
    # (5 s plus tard) ; il n'imprime ARMED qu'alors. Noeud note AVANT l'appel : un SSH coupe apres l'armement
    # laisserait une coupure en route, fail() tente donc `restore` aussi sur lui (filet : le timer de la cible).
    STOPPED+=("$n")
    armed=$(ssh_node "$n" "sudo -n /usr/local/bin/alarm-chaos-net cut $((T * 60))") || true
    [[ "$armed" == "ARMED" ]] || fail "alarm-chaos-net cut KO ou SSH interrompu sur $n (restauration tentee, filet : timer de la cible)"
    log "$n : wg0 coupe dans 5 s pendant $T min (restauration par timer sur le noeud)"
    continue
  fi
  cids=$(ssh_node "$n" "docker ps -q --filter label=com.docker.compose.project=$n" | xargs) || fail "ssh / docker ps KO sur $n"
  [[ -n "$cids" ]] || fail "aucun conteneur en cours sur $n"
  CIDS_OF[$n]=$cids
  # INV-178, avant tout arret : restart=always (Docker relance au demarrage du daemon un conteneur « always »
  # meme arrete a la main, pas un « unless-stopped ») puis restauration armee SUR LA CIBLE, detachee de la
  # session SSH ; ARMED n'est imprime que si elle tourne encore 1 s apres son lancement.
  armed=$(ssh_node "$n" "docker update --restart=always $cids >/dev/null || exit 1; setsid nohup sh -c 'sleep $(( DEADLINE + 60 - $(date +%s) )); docker start $cids; docker update --restart=unless-stopped $cids' alarm-chaos-$ID >/dev/null 2>&1 </dev/null & sleep 1; kill -0 \$! && echo ARMED") || true
  [[ "$armed" == "ARMED" ]] || fail "armement de la restauration KO sur $n : rien n'est arrete sur ce noeud"
  # Nouvelle session : le filet a survecu a la fin de la premiere (systemd-logind KillUserProcesses=yes le tuerait).
  ssh_node "$n" "pgrep -f '[a]larm-chaos-$ID\$' >/dev/null" \
    || fail "restauration armee disparue apres la session SSH sur $n (KillUserProcesses ?) : rien n'est arrete sur ce noeud"
  # Annulation ou alarme depuis la reclamation (sondes, armement, noeud precedent) : on n'arrete rien de plus.
  abort=$(abort_reason)
  [[ -z "$abort" ]] || fail "$abort : rien n'est arrêté sur $n"
  ssh_node "$n" "docker stop $cids" || fail "docker stop KO sur $n"
  STOPPED+=("$n")
  log "$n arrete : $cids"
done

# Pendant la panne, toutes les 10 s : leader Patroni (leader_changed) et arret anticipe (annulation admin, alarme).
POLL=$(( MINUTE_SECONDS < 10 ? MINUTE_SECONDS : 10 ))
LEADER=$LEADER_BEFORE
abort=""
while :; do
  left=$(( DEADLINE - $(date +%s) ))
  (( left > 0 )) || break
  sleep $(( left < POLL ? left : POLL ))
  now_leader=$(cluster_leader)
  if [[ -n "$now_leader" && "$now_leader" != "$LEADER" ]]; then
    log "leader Patroni : ${LEADER:-aucun} -> $now_leader"
    post_event leader_changed "{\"from\":\"$LEADER\",\"to\":\"$now_leader\"}"
    LEADER=$now_leader
  fi
  abort=$(abort_reason)
  if [[ -n "$abort" ]]; then
    log "Arret anticipe : $abort"
    post_event early_restore "{\"reason\":$(json_str "$abort")}"
    break
  fi
done

wait_healthy() {  # $1 noeud : /health 200 ET membre Patroni sain (member_ok), en <= HEALTHY_TIMEOUT s apres max(maintenant, T0)
  local now
  now=$(date +%s)
  local deadline=$(( (T0 > now ? T0 : now) + HEALTHY_TIMEOUT ))
  while (( $(date +%s) < deadline )); do
    if curl -fsS --max-time 3 "http://${NODE_IP[$1]}:8000/health" >/dev/null 2>&1 && member_ok "$1"; then
      return 0
    fi
    sleep 5
  done
  return 1
}

T0=$(date +%s)
if [[ "$MODE" == stop ]]; then
  for n in "${STOPPED[@]}"; do
    restore_node "$n" || fail "docker start KO sur $n (restauration armee sur le noeud en filet)"
  done
elif [[ -n "$abort" ]]; then
  # netcut, arret anticipe : wg0 remis tout de suite si la cible repond encore ; sinon (cas normal : ce runner
  # passe par wg0) son timer le remet a l'echeance, et l'attente de sante court depuis l'echeance.
  for n in "${STOPPED[@]}"; do
    restore_now "$n" || { log "$n injoignable : wg0 remis par le timer de la cible a l'echeance"; T0=$DEADLINE; }
  done
fi  # netcut a l'echeance : rien a faire a distance (tunnel coupe), le timer de la cible restaure
for n in "${STOPPED[@]}"; do
  wait_healthy "$n" || fail "$n pas sain ${HEALTHY_TIMEOUT} s apres la fin de la panne"
done
RESTORE_SECONDS=$(( $(date +%s) - T0 ))
(( RESTORE_SECONDS > 0 )) || RESTORE_SECONDS=0
post_event restored "{\"leader_after\":\"$(cluster_leader)\",\"restore_seconds\":$RESTORE_SECONDS}"
log "Panne controlee #$ID terminee : $NODES restaure(s) en $RESTORE_SECONDS s"
exit 0
