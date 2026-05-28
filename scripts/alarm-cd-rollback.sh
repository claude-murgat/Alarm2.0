#!/bin/bash
# alarm-cd-rollback.sh — rollback effectif d'1 noeud vers :stable-prev.
#
# Cf docs/CD_DESIGN.md §5 (Auto-rollback). Appele soit :
#   - Automatiquement par alarm-cd-canary.sh quand le seuil de fail est atteint
#   - Manuellement par l'humain en runbook ("ce noeud va mal, rollback")
#
# Mecanique :
#   1. SSH vers la cible.
#   2. Verifie que le tag local :stable-prev existe (= alarm-cd-canary.sh l'a pose
#      AVANT le restart courant).
#   3. Re-tag local :stable -> ancien digest via `docker tag :stable-prev :stable`.
#   4. docker compose up -d (re-cree les containers avec l'image re-taggee).
#   5. POST kind=rollback, status=success dans deployment_events.
#
# Idempotent : si :stable-prev n'existe pas (cas cold start, jamais promu avant),
# le script log un warning et exit 1 sans rien casser.

set -euo pipefail

NODE=""
SSH_USER="${SSH_USER:-alarm}"
REMOTE_REPO="${REMOTE_REPO:-/opt/alarm}"
API_BASE_DEFAULT="http://10.99.0.1:8000"
API_BASE="${API_BASE:-}"
REGISTRY="${REGISTRY:-ghcr.io/claude-murgat}"
GATEWAY_KEY="${GATEWAY_KEY:-}"
IMAGE="${IMAGE:-alarm-backend}"
TRIGGER="${TRIGGER:-manual}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --node)        NODE="$2"; shift 2 ;;
    --image)       IMAGE="$2"; shift 2 ;;
    --api-base)    API_BASE="$2"; shift 2 ;;
    --gateway-key) GATEWAY_KEY="$2"; shift 2 ;;
    --trigger)     TRIGGER="$2"; shift 2 ;;
    -h|--help)
      cat <<'HELP'
Usage : alarm-cd-rollback.sh --node <1|2|3> [options]

Options :
  --node <1|2|3>     Noeud cible (requis).
  --image <name>     Image a rollback (default: alarm-backend ; pas alarm-patroni
                     par defaut, cf §5 doc — Postgres rollback trop risque).
  --api-base <url>   API backend pour POST events.
  --gateway-key <k>  X-Gateway-Key (sinon env GATEWAY_KEY).
  --trigger <name>   Raison (manual | health_503 | restart_loop | replication_lag).
HELP
      exit 0
      ;;
    *) echo "Argument inconnu : $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$NODE" || ! "$NODE" =~ ^[1-3]$ ]]; then
  echo "ERREUR : --node requis" >&2
  exit 1
fi
if [[ -z "$GATEWAY_KEY" ]]; then
  echo "ERREUR : GATEWAY_KEY non definie" >&2
  exit 1
fi

case "$NODE" in
  1) WG_IP="10.99.0.2"; NODE_NAME="node1" ;;
  2) WG_IP="10.99.0.3"; NODE_NAME="node2" ;;
  3) WG_IP="10.99.0.1"; NODE_NAME="node3" ;;
esac

log() { echo "[rollback-$NODE_NAME] $*" >&2; }

# Decouvre le leader Patroni via GET /health role=primary sur les 3 IPs WG.
# POST /api/deployments/events requiert le leader (replicas -> 503 'replica').
# Quand appele depuis canary.sh, --api-base est deja resolu -> on skip ce bloc.
discover_leader() {
  local ip role
  for ip in 10.99.0.1 10.99.0.2 10.99.0.3; do
    role=$(curl -fsS --max-time 3 "http://${ip}:8000/health" 2>/dev/null \
      | python3 -c "import sys,json;print(json.load(sys.stdin).get('role',''))" 2>/dev/null \
      || echo "")
    if [[ "$role" == "primary" ]]; then
      echo "$ip"
      return 0
    fi
  done
  return 1
}

if [[ -z "$API_BASE" ]]; then
  if leader_ip=$(discover_leader); then
    API_BASE="http://${leader_ip}:8000"
    log "Leader Patroni detecte : $API_BASE"
  else
    API_BASE="$API_BASE_DEFAULT"
    log "WARN : aucun leader Patroni joignable, fallback $API_BASE (POST events risque 503)"
  fi
fi

ssh_target() {
  ssh -o BatchMode=yes -o ConnectTimeout=10 \
      "${SSH_USER}@${WG_IP}" "$@"
}

# --- 1. Verif que :stable-prev existe sur la cible ---
log "Verif tag :stable-prev sur la cible..."
PREV_DIGEST=$(ssh_target \
  "docker image inspect ${REGISTRY}/${IMAGE}:stable-prev \
     -f '{{if .RepoDigests}}{{index .RepoDigests 0}}{{end}}'" 2>/dev/null \
  | sed 's/.*@//' || echo "")

if [[ -z "$PREV_DIGEST" ]]; then
  log "ERREUR : pas de tag :stable-prev local sur la cible."
  log "  Cause probable : c'est le 1er deploiement (jamais de canary avant),"
  log "  ou un rollback precedent a deja consomme :stable-prev."
  log "  Action humaine : choisir un sha precis et faire `docker pull <repo>:sha-<short>`"
  log "  puis re-tag manuellement :stable."

  # --retry-all-errors absorbe transients + cas commun "leader Patroni
  # change pendant le restart" (cf canary.sh post_event() pour le contexte).
  curl --retry 3 --retry-delay 3 --retry-all-errors --max-time 15 \
    -fsS -X POST "$API_BASE/api/deployments/events" \
    -H "X-Gateway-Key: $GATEWAY_KEY" \
    -H "Content-Type: application/json" \
    -d "{
      \"node\": \"$NODE_NAME\",
      \"image\": \"$IMAGE\",
      \"kind\": \"abort\",
      \"status\": \"failure\",
      \"actor\": \"auto-rollback\",
      \"details\": {\"reason\": \"no_stable_prev\", \"trigger\": \"$TRIGGER\"}
    }" >/dev/null || true

  exit 1
fi

log "Tag :stable-prev local trouve : $PREV_DIGEST"

# Capture le digest :stable courant (= le buggue qu'on remplace), pour traçabilite.
CURRENT_DIGEST=$(ssh_target \
  "docker image inspect ${REGISTRY}/${IMAGE}:stable \
     -f '{{if .RepoDigests}}{{index .RepoDigests 0}}{{end}}'" 2>/dev/null \
  | sed 's/.*@//' || echo "")

# --- 2. Re-tag local :stable <- :stable-prev ---
log "Re-tag local : ${REGISTRY}/${IMAGE}:stable <- :stable-prev"
ssh_target "docker tag ${REGISTRY}/${IMAGE}:stable-prev ${REGISTRY}/${IMAGE}:stable"

# --- 3. Restart les containers via compose ---
# docker compose up -d (sans pull, sans build) re-cree les containers avec
# l'image actuelle (qui est maintenant :stable-prev re-taggee :stable).
log "docker compose up -d (sans pull, sans build)..."
ssh_target "cd $REMOTE_REPO && docker compose --env-file .env.prod.node${NODE} -p node${NODE} up -d"

# --- 4. POST event ---
DETAILS=$(cat <<JSON
{"trigger": "$TRIGGER", "previous": "$CURRENT_DIGEST", "rolled_back_to": "$PREV_DIGEST"}
JSON
)
curl --retry 3 --retry-delay 3 --retry-all-errors --max-time 15 \
  -fsS -X POST "$API_BASE/api/deployments/events" \
  -H "X-Gateway-Key: $GATEWAY_KEY" \
  -H "Content-Type: application/json" \
  -d "{
    \"node\": \"$NODE_NAME\",
    \"image\": \"$IMAGE\",
    \"kind\": \"rollback\",
    \"from_digest\": \"$CURRENT_DIGEST\",
    \"to_digest\": \"$PREV_DIGEST\",
    \"status\": \"success\",
    \"actor\": \"auto-rollback\",
    \"details\": $DETAILS
  }" >/dev/null || log "WARN : POST event rollback a echoue"

log "Rollback termine. La cible tourne maintenant sur $PREV_DIGEST."
log "ACTION HUMAINE : verifier les logs, decider si on garde ou on debug le sha :stable casse."
log "  Le canary est HALT par construction — les autres noeuds restent sur leur version actuelle."
exit 0
