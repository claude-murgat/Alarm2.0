#!/bin/bash
# alarm-cd-canary.sh — execute une etape canary pour UN noeud du cluster.
#
# Cf docs/CD_DESIGN.md §4 (Ordre canary) + §6 (Observabilite). Ce script :
#   1. Lit le digest local courant + le digest distant `:stable` sur la cible.
#   2. Si different, POST kind=canary_start dans deployment_events.
#   3. SSH vers la cible et lance `start-prod-node.sh --pull <N>`.
#   4. Poll `/health` pendant le soak (default 600s, sample 30s).
#   5. Si soak vert -> POST kind=canary_promoted, exit 0.
#   6. Si soak rouge -> POST kind=rollback (PR 6 enrichit avec l'action),
#      halt canary, exit 2.
#
# Designe pour tourner sur NODE3 cloud OVH (le seul qui voit les 3 noeuds via WG).
# L'humain enchaine les 3 commandes manuellement V1 :
#   ./scripts/alarm-cd-canary.sh --node 3   # NODE3 d'abord
#   ./scripts/alarm-cd-canary.sh --node 2   # onsite-2 ensuite
#   ./scripts/alarm-cd-canary.sh --node 1   # onsite-1 en dernier (le plus critique)
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
API_BASE="${API_BASE:-http://10.99.0.1:8000}"
REGISTRY="${REGISTRY:-ghcr.io/claude-murgat}"
TAG="${TAG:-stable}"
SSH_USER="${SSH_USER:-alarm}"
REMOTE_REPO="${REMOTE_REPO:-/opt/alarm}"
DRY_RUN=0
GATEWAY_KEY="${GATEWAY_KEY:-}"

# --- Args ---
while [[ $# -gt 0 ]]; do
  case "$1" in
    --node)        NODE="$2"; shift 2 ;;
    --soak)        SOAK_SECONDS="$2"; shift 2 ;;
    --api-base)    API_BASE="$2"; shift 2 ;;
    --gateway-key) GATEWAY_KEY="$2"; shift 2 ;;
    --dry-run)     DRY_RUN=1; shift ;;
    -h|--help)
      cat <<'HELP'
Usage : alarm-cd-canary.sh --node <1|2|3> [options]

Options :
  --node <1|2|3>     Numero du noeud cible (requis).
  --soak <secs>      Duree du soak post-restart (default: 600 = 10 min).
  --api-base <url>   API backend pour POST events (default: http://10.99.0.1:8000).
  --gateway-key <k>  X-Gateway-Key pour POST events (sinon lit env GATEWAY_KEY).
  --dry-run          Affiche les actions sans les executer.
HELP
      exit 0
      ;;
    *) echo "Argument inconnu : $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$NODE" || ! "$NODE" =~ ^[1-3]$ ]]; then
  echo "ERREUR : --node <1|2|3> requis" >&2
  exit 1
fi

if [[ -z "$GATEWAY_KEY" ]]; then
  echo "ERREUR : GATEWAY_KEY non definie (option --gateway-key ou env)" >&2
  exit 1
fi

# Mapping node -> IP WG. Cohérent avec .env.prod.node{1,2,3} :
#   node1 -> onsite-1 -> 10.99.0.2
#   node2 -> onsite-2 -> 10.99.0.3
#   node3 -> NODE3 cloud -> 10.99.0.1
case "$NODE" in
  1) WG_IP="10.99.0.2"; NODE_NAME="node1" ;;
  2) WG_IP="10.99.0.3"; NODE_NAME="node2" ;;
  3) WG_IP="10.99.0.1"; NODE_NAME="node3" ;;
esac

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

ssh_target() {
  ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new \
      "${SSH_USER}@${WG_IP}" "$@"
}

# --- 1. Verif pre-requis ---
log "Verification pre-requis..."
if ! ssh_target "true" 2>/dev/null; then
  log "ERREUR : SSH vers ${SSH_USER}@${WG_IP} echoue (cle manquante ? noeud down ?)"
  exit 1
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
  exit 1
fi

if [[ "$LOCAL_DIGEST_BACKEND" == "$REMOTE_DIGEST_BACKEND" ]]; then
  log "Pas de mise a jour : digest local == distant ($LOCAL_DIGEST_BACKEND). Exit 0."
  exit 0
fi

log "Diff detecte alarm-backend : local=${LOCAL_DIGEST_BACKEND:-none} -> remote=$REMOTE_DIGEST_BACKEND"

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
  else
    curl -fsS -X POST "$API_BASE/api/deployments/events" \
      -H "X-Gateway-Key: $GATEWAY_KEY" \
      -H "Content-Type: application/json" \
      -d "$body" >/dev/null || log "WARN : POST event $kind a echoue"
  fi
}

log "POST kind=canary_start"
post_event "canary_start" "in_progress" "{\"soak_seconds\": $SOAK_SECONDS}"

# --- 3. Trigger restart sur la cible ---
log "SSH vers la cible, lance start-prod-node.sh --pull $NODE"
if ! run "ssh_target \"cd $REMOTE_REPO && ./scripts/start-prod-node.sh --pull $NODE\""; then
  log "ERREUR : start-prod-node.sh a echoue. Halt canary."
  post_event "abort" "failure" "{\"phase\": \"start-prod-node\"}"
  exit 2
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
      log "Seuil atteint : $FAILS polls 503 consecutifs. Halt + signal rollback."
      post_event "rollback" "failure" \
        "{\"trigger\": \"health_503\", \"consecutive_fails\": $FAILS, \"total_checks\": $TOTAL_CHECKS}"
      # Le rollback effectif (re-tag + restart) est ajoute par PR 6.
      # Ici on signale juste, on n'agit pas.
      log "Rollback signal envoye. PR 6 prendra le relais (V1 : intervention humaine)."
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
