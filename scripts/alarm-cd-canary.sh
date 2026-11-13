#!/bin/bash
# alarm-cd-canary.sh — execute une etape canary pour UN noeud du cluster.
#
# Cf docs/CD_DESIGN.md §4 (Ordre canary) + §6 (Observabilite). Ce script :
#   1. Lit le digest local courant + le digest distant `:stable` sur la cible.
#   2. Si different et la cible est le leader Patroni : bascule planifiee vers un replica
#      sain (POST /switchover, kind=switchover) ; pas de bascule terminee -> exit 3 (INV-187).
#      Puis POST kind=canary_start dans deployment_events.
#   3. Sauve les 300 dernieres lignes du backend sur la cible, puis SSH vers la cible et lance
#      `start-prod-node.sh --pull <N>`.
#   3b. Apres une bascule et apres le redemarrage : un backend primary en PRIMARY_TIMEOUT s ; sinon seconde
#      bascule, email et exit 2 (INV-196). Avant de redemarrer une cible replica, pas de primary -> exit 3.
#   4. Poll `/health` pendant le soak (default 600s, sample 30s).
#   5. Si soak vert -> POST kind=canary_promoted, exit 0.
#   6. Si soak rouge -> POST kind=rollback (PR 6 enrichit avec l'action),
#      halt canary, exit 2.
#   Prerequis KO (SSH, digest distant illisible), rien n'est touche -> exit 3 (reporte par l'orchestrateur).
#   Cible leader Patroni sans bascule terminee (INV-187) -> exit 3, cible pas redemarree.
#
# Designe pour tourner sur NODE3 cloud OVH (le seul qui voit les 7 noeuds via WG).
# L'humain enchaine les 7 commandes manuellement V1 (ordre de alarm-cd-orchestrate.sh) :
#   ./scripts/alarm-cd-canary.sh --node 3   # NODE3 d'abord
#   ./scripts/alarm-cd-canary.sh --node 7   # alarm-4 (non votant, futur Izeron)
#   ./scripts/alarm-cd-canary.sh --node 6   # alarm-3 (non votant, futur Izeron)
#   ./scripts/alarm-cd-canary.sh --node 5   # alarm-2 (non votant)
#   ./scripts/alarm-cd-canary.sh --node 4   # alarm-1 (non votant)
#   ./scripts/alarm-cd-canary.sh --node 2   # onsite-2 ensuite
#   ./scripts/alarm-cd-canary.sh --node 1   # onsite-1 en dernier (le plus critique)
# Noeuds 4 a 7 : start-prod-node.sh ne relance que Patroni + backend (pas d'etcd local).
#
# V1.5 (cf doc) : promotion auto entre etapes apres 5 deploiements clean.
#
# Pre-requis :
# - SSH par cle vers alarm@10.99.0.X depuis cette machine (sans password).
# - GATEWAY_KEY definie en env (meme valeur que le backend cible).
# - Timer pull (alarm-cd-pull.timer) actif sur la cible -> image deja en cache.

set -euo pipefail

# --- Defaults ---
NODE=""
SOAK_SECONDS=600           # 10 min, cf §4 doc
SAMPLE_INTERVAL=30         # poll /health toutes les 30s
FAILURE_THRESHOLD=3        # 3 polls 503 consecutifs -> rollback
API_BASE_DEFAULT="http://10.99.0.1:8000"
API_BASE="${API_BASE:-}"
REGISTRY="${REGISTRY:-ghcr.io/claude-murgat}"
TAG="${TAG:-stable}"
SSH_USER="${SSH_USER:-alarm}"
REMOTE_REPO="${REMOTE_REPO:-/opt/alarm}"
DRY_RUN=0
GATEWAY_KEY="${GATEWAY_KEY:-}"
PENDING=""                 # noeuds encore a redemarrer dans ce passage (orchestrateur)
VOTERS="1 2 3"             # votants etcd (INV-090), passes par l'orchestrateur
SWITCHOVER_TIMEOUT="${SWITCHOVER_TIMEOUT:-60}"  # bascule Patroni (INV-187), env pour les tests
# Backend primary apres une bascule ou un redemarrage (INV-196) : ~7 s attendus (boucle de 5 s du backend).
PRIMARY_TIMEOUT="${PRIMARY_TIMEOUT:-60}"
MAILER="${MAILER:-/opt/alarm/send-alert-email.py}"  # SMTP de NODE3, marche sans base inscriptible (INV-196)
LOG_DIR="${CD_LOG_DIR:-/var/lib/alarm/cd/logs}"      # logs du backend, sur la machine du noeud (INV-196)

# --- Args ---
while [[ $# -gt 0 ]]; do
  case "$1" in
    --node)        NODE="$2"; shift 2 ;;
    --soak)        SOAK_SECONDS="$2"; shift 2 ;;
    --api-base)    API_BASE="$2"; shift 2 ;;
    --gateway-key) GATEWAY_KEY="$2"; shift 2 ;;
    --pending)     PENDING="$2"; shift 2 ;;
    --voters)      VOTERS="$2"; shift 2 ;;
    --dry-run)     DRY_RUN=1; shift ;;
    -h|--help)
      cat <<'HELP'
Usage : alarm-cd-canary.sh --node <1-7> [options]

Options :
  --node <1-7>       Numero du noeud cible (requis).
  --soak <secs>      Duree du soak post-restart (default: 600 = 10 min).
  --api-base <url>   API backend pour POST events (default: http://10.99.0.1:8000).
  --gateway-key <k>  X-Gateway-Key pour POST events (sinon lit env GATEWAY_KEY).
  --pending "<N..>"  Noeuds encore a redemarrer dans ce passage : evites comme nouveau
                     leader si la cible est le leader Patroni (default: aucun).
  --voters "<N..>"   Votants etcd, preferes comme nouveau leader (default: "1 2 3").
  --dry-run          Affiche les actions sans les executer.
HELP
      exit 0
      ;;
    *) echo "Argument inconnu : $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$NODE" || ! "$NODE" =~ ^[1-7]$ ]]; then
  echo "ERREUR : --node <1-7> requis" >&2
  exit 1
fi

if [[ -z "$GATEWAY_KEY" ]]; then
  echo "ERREUR : GATEWAY_KEY non definie (option --gateway-key ou env)" >&2
  exit 1
fi

# Mapping node -> IP WG. Cohérent avec .env.prod.node{1..7} :
#   node1 -> onsite-1 -> 10.99.0.2
#   node2 -> onsite-2 -> 10.99.0.3
#   node3 -> NODE3 cloud -> 10.99.0.1
#   node4 -> alarm-1 -> 10.99.0.4 (non votant)
#   node5 -> alarm-2 -> 10.99.0.5 (non votant)
#   node6 -> alarm-3 -> 10.99.0.6 (non votant, futur Izeron)
#   node7 -> alarm-4 -> 10.99.0.7 (non votant, futur Izeron)
node_ip() {
  case "$1" in node1) echo 10.99.0.2 ;; node2) echo 10.99.0.3 ;; node3) echo 10.99.0.1 ;; *) echo "10.99.0.${1#node}" ;; esac
}
NODE_NAME="node$NODE"
WG_IP=$(node_ip "$NODE_NAME")

TARGET_HEALTH="http://${WG_IP}:8000/health"

log() {
  echo "[canary-$NODE_NAME] $*" >&2
}

run() {
  # Helper : execute en respectant --dry-run.
  if (( DRY_RUN )); then
    echo "DRY-RUN > $*"
  else
    eval "$*"
  fi
}

ssh_to() {
  ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new \
      "${SSH_USER}@$1" "${@:2}"
}
ssh_target() { ssh_to "$WG_IP" "$@"; }

# IP du backend primary via GET /health des 7 IPs WG : role=primary et cluster_writable, et un seul
# (deux primary = aucun, INV-196). POST /api/deployments/events requiert le leader (replicas -> 503 'replica').
discover_leader() {
  local ip found=""
  for ip in 10.99.0.1 10.99.0.2 10.99.0.3 10.99.0.4 10.99.0.5 10.99.0.6 10.99.0.7; do
    if curl -fsS --max-time 3 "http://${ip}:8000/health" 2>/dev/null | python3 -c \
        "import sys,json;h=json.load(sys.stdin);sys.exit(not(h.get('role')=='primary' and h.get('cluster_writable') is True))" \
        2>/dev/null; then
      [[ -z "$found" ]] || return 1
      found=$ip
    fi
  done
  [[ -n "$found" ]] && echo "$found"
}

# Resolution API_BASE : (1) --api-base CLI, (2) env $API_BASE, (3) discover_leader,
# (4) fallback default (log WARN, risque 503 si non-leader).
if [[ -z "$API_BASE" ]]; then
  if leader_ip=$(discover_leader); then
    API_BASE="http://${leader_ip}:8000"
    log "Leader Patroni detecte : $API_BASE"
  else
    API_BASE="$API_BASE_DEFAULT"
    log "WARN : aucun leader Patroni joignable, fallback $API_BASE (POST events risque 503)"
  fi
fi

# --- 1. Verif pre-requis ---
log "Verification pre-requis..."
if ! ssh_target "true" 2>/dev/null; then
  log "ERREUR : SSH vers ${SSH_USER}@${WG_IP} echoue (cle manquante ? noeud down ?)"
  exit 3
fi

# Image presente en cache local sur la cible (= timer pull a deja tourne) ?
LOCAL_DIGEST_BACKEND=$(ssh_target \
  "docker image inspect ${REGISTRY}/alarm-backend:${TAG} \
     -f '{{if .RepoDigests}}{{index .RepoDigests 0}}{{end}}'" 2>/dev/null \
  | sed 's/.*@//' || echo "")
REMOTE_DIGEST_BACKEND=$(ssh_target \
  "docker buildx imagetools inspect ${REGISTRY}/alarm-backend:${TAG} \
     --format '{{.Manifest.Digest}}'" 2>/dev/null || echo "")

if [[ -z "$REMOTE_DIGEST_BACKEND" ]]; then
  log "ERREUR : impossible de lire le digest distant alarm-backend:${TAG}"
  exit 3
fi

if [[ "$LOCAL_DIGEST_BACKEND" == "$REMOTE_DIGEST_BACKEND" ]]; then
  # Le cache local est a jour vs le registry — mais ca ne suffit pas a conclure
  # "rien a faire" : le pull-timer (alarm-cd-pull.timer) tourne toutes les 5 min
  # et a tres bien pu mettre a jour le cache SANS redemarrer le container. Le
  # container peut donc tourner sur l'ancienne version meme si cache == registry.
  #
  # On compare l'image-id du container backend en execution avec l'image-id du
  # tag :stable cache localement. Si different (= container vieux), on continue
  # le canary (start-prod-node.sh --pull fera un docker compose up -d qui
  # detectera le mismatch et restartera). Si meme (= container deja a jour),
  # rien a faire pour de vrai.
  #
  # Cas auto-orchestre (V1.5) : `alarm-cd-orchestrate.sh` appelle ce script
  # apres que pull-timer a deja pulle, ce qui aboutit systematiquement sur cette
  # branche. Sans la verif container, le canary V1.5 serait un no-op silencieux
  # qui laisserait la prod sur l'ancienne version. Cf docs/CD_DESIGN.md §4
  # (V1.5 — promotion auto entre etapes).
  CACHED_IMG_ID=$(ssh_target \
    "docker image inspect ${REGISTRY}/alarm-backend:${TAG} -f '{{.Id}}'" \
    2>/dev/null || echo "")
  RUNNING_IMG_ID=$(ssh_target \
    "docker compose --env-file ${REMOTE_REPO}/.env.prod.node${NODE} -p node${NODE} ps -q backend 2>/dev/null \
       | head -1 \
       | xargs -r docker inspect -f '{{.Image}}' 2>/dev/null" \
    || echo "")
  RUNNING_IMG_ID=$(echo "$RUNNING_IMG_ID" | head -1)

  if [[ -n "$RUNNING_IMG_ID" && "$RUNNING_IMG_ID" == "$CACHED_IMG_ID" ]]; then
    log "Pas de mise a jour : cache == registry ET container deja sur :stable ($LOCAL_DIGEST_BACKEND). Exit 0."
    exit 0
  fi
  log "Cache :stable a jour mais container tourne sur ancienne image (running=$RUNNING_IMG_ID, cached=$CACHED_IMG_ID). Restart..."
fi

log "Diff detecte alarm-backend : local=${LOCAL_DIGEST_BACKEND:-none} -> remote=$REMOTE_DIGEST_BACKEND"

# --- 1b. Preserver :stable courant en :stable-prev AVANT le restart ---
# Permet a alarm-cd-rollback.sh (PR 6) de revenir a cet etat en cas de fail.
# Si LOCAL_DIGEST_BACKEND vide (premiere installation), on skip — pas de
# rollback possible, c'est OK (cas cold start documente §5 Q5.2 du doc).
if [[ -n "$LOCAL_DIGEST_BACKEND" ]]; then
  log "Preservation :stable-prev sur la cible (pour rollback futur)"
  run "ssh_target \"docker tag ${REGISTRY}/alarm-backend:${TAG} ${REGISTRY}/alarm-backend:stable-prev\"" \
    || log "WARN : tag :stable-prev a echoue (rollback non disponible pour ce canary)"
fi

# --- 2. POST canary_start ---
post_event() {
  local kind="$1"
  local status="$2"
  local details="${3:-}"

  local body
  body=$(cat <<JSON
{
  "node": "${NODE_NAME}",
  "image": "alarm-backend",
  "kind": "${kind}",
  "from_digest": "${LOCAL_DIGEST_BACKEND:-}",
  "to_digest": "${REMOTE_DIGEST_BACKEND}",
  "status": "${status}",
  "actor": "orchestrator-canary",
  "details": ${details:-null}
}
JSON
)
  if (( DRY_RUN )); then
    echo "DRY-RUN POST $API_BASE/api/deployments/events :"
    echo "$body"
    return 0
  fi

  # Robustesse aux failovers Patroni pendant le canary :
  # - curl --retry-all-errors couvre les transients (5xx, timeout, connection reset)
  # - Si fail malgre retry curl -> probable failover avec leader change
  #   -> re-discover et 2e tentative avec nouvelle API_BASE.
  # Sans ce wrapper, le scenario "restart node3 -> failover Patroni -> POST
  # canary_promoted vers ex-leader" (observe en prod 2026-05-28) perdait tous
  # les events suivants de la chaine.
  local attempt
  for attempt in 1 2; do
    if curl --retry 3 --retry-delay 3 --retry-all-errors --max-time 15 \
            -fsS -X POST "$API_BASE/api/deployments/events" \
            -H "X-Gateway-Key: $GATEWAY_KEY" \
            -H "Content-Type: application/json" \
            -d "$body" >/dev/null 2>&1; then
      return 0
    fi
    if [[ $attempt -eq 1 ]]; then
      log "POST $kind echec apres curl --retry, re-discover leader..."
      local new_leader
      if new_leader=$(discover_leader); then
        if [[ "$API_BASE" != "http://${new_leader}:8000" ]]; then
          log "Failover Patroni detecte : $API_BASE -> http://${new_leader}:8000"
          API_BASE="http://${new_leader}:8000"
        else
          log "Leader inchange ($API_BASE), 2e tentative inutile"
          break
        fi
      else
        log "discover_leader echoue, 2e tentative inutile"
        break
      fi
    fi
  done
  log "WARN : POST event $kind a echoue definitivement"
}

# --- 2a. Cible leader Patroni : bascule planifiee AVANT le restart (INV-187) ---
# Redemarrer le leader = le perdre brutalement : ~15-35 s sans ecriture, nouvelle timeline,
# et les replicas hors ligne a ce moment reviennent sur l'ancienne, WAL supprimes (re-clonage,
# INV-168 ; prod 2026-11-05/06, timeline 285 -> 290 en un jour). Le leader passe d'abord la
# main a un replica sain ; sans bascule terminee il n'est PAS redemarre : exit 3, reporte.
#
# Vue du cluster (GET :8008/cluster) : "<leader> <role de la cible> <candidat>" ("-" = aucun).
# Candidat : replica `streaming` sur la timeline du leader, pas `nofailover` ; d'abord un noeud
# qui n'est plus a redemarrer dans ce passage, puis un votant etcd, puis le plus petit lag.
CLUSTER_VIEW_PY='
import json, sys
members = json.load(sys.stdin)["members"]
target = sys.argv[1]
pending, voters = (["node" + n for n in a.split()] for a in sys.argv[2:4])
leader = next((m for m in members if m["role"] == "leader"), {})
tl = leader.get("timeline")
ok = [m for m in members if m["role"] != "leader" and m.get("state") == "streaming"
      and m.get("timeline", tl) == tl and not m.get("tags", {}).get("nofailover")]
ok.sort(key=lambda m: (m["name"] in pending, m["name"] not in voters,
                       m["lag"] if isinstance(m.get("lag"), int) else float("inf")))
role = next((m["role"] for m in members if m["name"] == target), "absent")
print(leader.get("name", "-"), role, ok[0]["name"] if ok else "-")
'
# Lu sur la cible, sinon sur un autre noeud (meme etat : il vient d'etcd).
cluster_view() {
  local ip
  for ip in "$WG_IP" 10.99.0.1 10.99.0.2 10.99.0.3 10.99.0.4 10.99.0.5 10.99.0.6 10.99.0.7; do
    curl -fsS --max-time 5 "http://${ip}:8008/cluster" 2>/dev/null \
      | python3 -c "$CLUSTER_VIEW_PY" "$NODE_NAME" "$PENDING" "$VOTERS" 2>/dev/null && return 0
  done
  return 1
}
# Bascule terminee : un leader elu ET la cible redevenue replica.
switched() {
  local role
  read -r PG_LEADER role _ <<< "$(cluster_view)"
  [[ "$PG_LEADER" != "-" && "$role" =~ ^(replica|sync_standby|quorum_standby)$ ]]
}
# Demandee a l'API Patroni du leader ; reponse journalisee, seul l'etat du cluster fait foi.
switchover() {  # <leader> <candidat>
  RESP=$(curl -sS --max-time "$SWITCHOVER_TIMEOUT" -X POST "http://$(node_ip "$1"):8008/switchover" \
    -H "Content-Type: application/json" \
    -d "{\"leader\": \"$1\", \"candidate\": \"$2\"}" 2>&1 || true)
  log "Patroni /switchover : $RESP"
}

# --- Backend primary (INV-196) ---
# Le backend du nouveau leader se dit primary a son tour suivant (boucle de 5 s, leader_election.py) ; celui de
# l'ancien leader peut encore le dire jusque-la : apres une bascule, son IP ne compte pas.
wait_primary() {  # [<IP de l'ancien leader>]
  local ip deadline=$(( $(date +%s) + PRIMARY_TIMEOUT ))
  until ip=$(discover_leader) && [[ "$ip" != "${1:-}" ]]; do
    if (( $(date +%s) >= deadline )); then return 1; fi
    sleep 2
  done
  API_BASE="http://${ip}:8000"  # les events suivent le primary
  log "Backend primary : $API_BASE"
}
# 300 dernieres lignes du backend d'un noeud, sauvees sur sa machine (perdues a la recreation) ; 10 gardees.
snapshot_logs() {  # <ip> <nodeN>
  local f="$LOG_DIR/$2-$(date -u +%Y%m%dT%H%M%SZ).log"
  if ssh_to "$1" "mkdir -p $LOG_DIR && docker logs --timestamps --tail 300 $2-backend-1 > $f 2>&1 \
      && { ls -1t $LOG_DIR/*.log | tail -n +11 | xargs -r rm -f --; }" 2>/dev/null; then
    log "Logs du backend $2 sauves : $1:$f"
  else
    log "WARN : logs du backend $2 non sauves ($1:$f)"
  fi
}
# Aucun backend primary : une seule seconde bascule, nouvelle attente, puis arret du passage et email quoi
# qu'il arrive (le backend ne peut pas stocker d'event sans primary). Plus aucun redemarrage.
no_primary() {  # <ce qui vient d'etre fait>
  local leader cand retry="aucune" found=0
  log "ERREUR : aucun backend primary ${PRIMARY_TIMEOUT}s apres $1"
  read -r leader _ cand <<< "$(cluster_view || echo "- - -")"
  if [[ "$leader" != "-" ]]; then snapshot_logs "$(node_ip "$leader")" "$leader"; fi
  if [[ "$leader" != "-" && "$cand" != "-" ]]; then
    log "Seconde bascule : $leader -> $cand"
    switchover "$leader" "$cand"
    retry="$leader -> $cand (Patroni : $RESP)"
    wait_primary "$(node_ip "$leader")" && found=1
  fi
  local result="TOUJOURS AUCUN backend primary : intervention requise"
  if (( found )); then result="backend primary retrouve : $API_BASE"; fi
  printf '%s\n' \
    "Deploiement CD arrete par alarm-cd-canary ($NODE_NAME, image $REMOTE_DIGEST_BACKEND)." "" \
    "Aucun backend n'a annonce role=primary sur /health ${PRIMARY_TIMEOUT}s apres $1 : le cluster ne pouvait plus ecrire." \
    "Leader Patroni : $leader. Logs de son backend : $LOG_DIR/ sur sa machine." \
    "Seconde bascule : $retry" \
    "Resultat : $result" "" \
    "Passage arrete : aucun autre noeud redemarre (repris apres le cooldown de 1 h, ou a la prochaine image)." \
    "A regarder : journalctl -u alarm-cd-orchestrate -n 200 sur NODE3, /health des 7 backends, docker logs ${leader}-backend-1." \
    "Cf INV-196 (tests/INVARIANTS.md), docs/CD_DESIGN.md §4." \
    | "$MAILER" "[ALARME-MURGAT] CD arrete : aucun backend primary apres $1" >&2 \
    || log "WARN : email d'alerte NON envoye ($MAILER)"
  if (( found )); then
    post_event "switchover" "failure" \
      "{\"reason\": \"no_primary\", \"after\": \"$1\", \"leader\": \"$leader\", \"candidate\": \"$cand\"}"
  fi
  log "Canary halt : $result. Les noeuds suivants ne sont PAS updates. Exit 2."
  exit 2
}

if ! VIEW=$(cluster_view); then
  log "ERREUR : etat Patroni (GET :8008/cluster) illisible : $NODE_NAME est peut-etre le leader, pas redemarre. Exit 3."
  exit 3
fi
read -r PG_LEADER _ CANDIDATE <<< "$VIEW"
if [[ "$PG_LEADER" == "$NODE_NAME" ]]; then
  if [[ "$CANDIDATE" == "-" ]]; then
    log "ERREUR : $NODE_NAME est le leader Patroni et aucun replica sain (streaming, meme timeline) ne peut le remplacer : pas redemarre. Exit 3."
    post_event "switchover" "failure" '{"reason": "no_candidate"}'
    exit 3
  fi
  log "$NODE_NAME est le leader Patroni : bascule vers $CANDIDATE avant le restart"
  if (( DRY_RUN )); then
    echo "DRY-RUN > POST http://${WG_IP}:8008/switchover leader=$NODE_NAME candidate=$CANDIDATE"
  else
    DEADLINE=$(( $(date +%s) + SWITCHOVER_TIMEOUT ))
    switchover "$NODE_NAME" "$CANDIDATE"
    until switched; do
      if (( $(date +%s) >= DEADLINE )); then
        log "ERREUR : bascule vers $CANDIDATE non terminee en ${SWITCHOVER_TIMEOUT}s : $NODE_NAME pas redemarre. Exit 3."
        post_event "switchover" "failure" "{\"candidate\": \"$CANDIDATE\", \"reason\": \"timeout\"}"
        exit 3
      fi
      sleep 2
    done
    log "Bascule faite : leader $PG_LEADER, $NODE_NAME replica"
    if ! wait_primary "$WG_IP"; then
      PENDING+=" $NODE"  # pas redemarre : encore sur l'ancienne image
      no_primary "la bascule de $NODE_NAME vers $PG_LEADER"
    fi
    post_event "switchover" "success" "{\"from\": \"$NODE_NAME\", \"to\": \"$PG_LEADER\"}"
  fi
elif ! wait_primary; then
  # Jamais de redemarrage tant qu'aucun backend n'ecrit (INV-196) : rien n'est touche, reporte.
  log "ERREUR : aucun backend primary en ${PRIMARY_TIMEOUT}s : $NODE_NAME pas redemarre. Exit 3."
  exit 3
fi

log "POST kind=canary_start"
post_event "canary_start" "in_progress" "{\"soak_seconds\": $SOAK_SECONDS}"

# --- 3. Trigger restart sur la cible ---
run "snapshot_logs $WG_IP $NODE_NAME"
log "SSH vers la cible, lance start-prod-node.sh --pull $NODE"
if ! run "ssh_target \"cd $REMOTE_REPO && ./scripts/start-prod-node.sh --pull $NODE\""; then
  log "ERREUR : start-prod-node.sh a echoue. Halt canary."
  post_event "abort" "failure" "{\"phase\": \"start-prod-node\"}"
  exit 2
fi
if (( ! DRY_RUN )) && ! wait_primary; then
  no_primary "le redemarrage de $NODE_NAME"
fi

# --- 4. Soak ---
log "Soak demarre pour ${SOAK_SECONDS}s (sample ${SAMPLE_INTERVAL}s, threshold ${FAILURE_THRESHOLD} polls 503 consec)"
END_TS=$(( $(date +%s) + SOAK_SECONDS ))
FAILS=0
TOTAL_CHECKS=0
TOTAL_FAILS=0

while [[ $(date +%s) -lt $END_TS ]]; do
  TOTAL_CHECKS=$((TOTAL_CHECKS + 1))

  if curl -fsS --max-time 10 "$TARGET_HEALTH" >/dev/null 2>&1; then
    FAILS=0
  else
    FAILS=$((FAILS + 1))
    TOTAL_FAILS=$((TOTAL_FAILS + 1))
    log "Health check ${TOTAL_CHECKS} echec (fails consec: $FAILS / $FAILURE_THRESHOLD)"

    if (( FAILS >= FAILURE_THRESHOLD )); then
      log "Seuil atteint : $FAILS polls 503 consecutifs. Halt + auto-rollback."

      # PR 6 : auto-rollback effectif. Le canary appelle le script de rollback
      # qui re-tag :stable <- :stable-prev sur la cible puis restart.
      # Note : alarm-cd-rollback.sh emet lui-meme le POST kind=rollback dans
      # deployment_events — on n'en emet pas un en double ici.
      SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
      if [[ -x "$SCRIPT_DIR/alarm-cd-rollback.sh" ]]; then
        log "Appel alarm-cd-rollback.sh --node $NODE --trigger health_503"
        if (( DRY_RUN )); then
          echo "DRY-RUN > $SCRIPT_DIR/alarm-cd-rollback.sh --node $NODE --trigger health_503"
        else
          "$SCRIPT_DIR/alarm-cd-rollback.sh" \
            --node "$NODE" \
            --trigger "health_503" \
            --api-base "$API_BASE" \
            --gateway-key "$GATEWAY_KEY" \
            || log "WARN : alarm-cd-rollback.sh a retourne une erreur (cf logs ci-dessus)"
        fi
      else
        log "WARN : alarm-cd-rollback.sh introuvable -> signal seul (intervention humaine requise)"
        post_event "rollback" "failure" \
          "{\"trigger\": \"health_503\", \"consecutive_fails\": $FAILS, \"total_checks\": $TOTAL_CHECKS, \"note\": \"rollback script missing\"}"
      fi

      log "Canary halt. Les noeuds suivants ne sont PAS updates."
      exit 2
    fi
  fi

  sleep "$SAMPLE_INTERVAL"
done

# --- 5. Soak vert ---
log "Soak termine sans incident. Promote."
post_event "canary_promoted" "success" \
  "{\"soak_seconds\": $SOAK_SECONDS, \"total_checks\": $TOTAL_CHECKS, \"transient_fails\": $TOTAL_FAILS}"

log "Canary $NODE_NAME OK. Tu peux enchainer sur le noeud suivant (cf §4 ordre du doc)."
exit 0
