#!/bin/bash
# alarm-cd-orchestrate.sh — chaine canary 3 -> 2 -> 1 automatique (V1.5).
#
# Cf docs/CD_DESIGN.md §4 (ordre canary) et §8 (plan PR 9 — "promotion canary
# auto apres soak vert"). Wraps alarm-cd-canary.sh pour les 3 noeuds en sequence,
# halt immediat sur le premier echec (l'auto-rollback du noeud cible est gere
# par alarm-cd-canary.sh -> alarm-cd-rollback.sh).
#
# Tourne via timer systemd `alarm-cd-orchestrate.timer` sur NODE3 cloud
# (le seul noeud qui voit les 3 cibles via le mesh WG). Le timer est livre
# **disable** par defaut — l'operateur l'active explicitement apres avoir
# valide 5 cycles canary manuels (cf §4 doc "V1.5 a activer apres 5
# deploiements clean").
#
# Idempotence :
#   - Compare le digest courant de :stable cache localement au digest du
#     dernier deploiement reussi (state file /var/lib/alarm/cd/orchestrate.state)
#   - Si meme digest + outcome=success precedent -> no-op silencieux (95 %
#     des ticks du timer).
#   - Si meme digest + outcome=failure precedent + cooldown < 1 h -> skip
#     (anti-boucle si :stable est casse).
#
# Pre-requis (cf docs/CD_DESIGN.md §3 + §4) :
#   - alarm-cd-pull.timer actif sur les 3 noeuds (pour que les images soient
#     en cache local AVANT l'orchestration).
#   - Cle SSH alarm@10.99.0.{1,2,3} fonctionnelle depuis NODE3.
#   - GATEWAY_KEY definie via /etc/alarm/cd.env (lu par le unit systemd).
#
# Sortie :
#   exit 0  -> rien a faire OU chaine canary complete reussie
#   exit 1  -> erreur preflight (SSH KO, GATEWAY_KEY manquante, etc.)
#   exit 2  -> chaine canary halt (au moins un noeud a fail/rollback) — l'event
#              est deja loggue par alarm-cd-canary.sh / alarm-cd-rollback.sh

set -euo pipefail

REGISTRY="${REGISTRY:-ghcr.io/claude-murgat}"
TAG="${TAG:-stable}"
IMAGE="${IMAGE:-alarm-backend}"
STATE_DIR="/var/lib/alarm/cd"
STATE_FILE="${STATE_DIR}/orchestrate.state"
# /run/alarm-cd-orchestrate/ est cree par systemd (RuntimeDirectory= dans le
# unit) avec owner alarm:alarm. Symetrique a alarm-cd-pull (cf service file).
LOCK_FILE="/run/alarm-cd-orchestrate/lock"
API_BASE_DEFAULT="http://10.99.0.1:8000"
API_BASE="${API_BASE:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Cooldown apres echec (rollback) : 1 h, anti-boucle si :stable cassé en boucle.
# Cf §5 doc — "hystérésis : pas de nouvelle tentative de pull :stable sur ce
# noeud pendant 1 h". Ici on porte ça au niveau orchestration (chaine entiere).
COOLDOWN_AFTER_FAIL=3600
# Soak entre etapes (override possible via env, default 600s = 10 min).
SOAK="${SOAK:-600}"
# Ordre canary fige : NODE3 (cloud, le moins critique) -> onsite-2 (Celeron,
# maillon faible) -> onsite-1 (i5, le plus puissant et souvent primary).
# Cf §4 doc — justification de l'ordre.
NODES_ORDER=(3 2 1)

log() {
  # stderr seul -> systemd capture via StandardError=journal (cf unit file).
  echo "[alarm-cd-orchestrate] $*" >&2
}

# Decouvre le leader Patroni via GET /health role=primary sur les 3 IPs WG.
# POST /api/deployments/events (par canary) requiert le leader (replicas -> 503).
# Sans ce bloc, --api-base default = 10.99.0.1 = NODE3 souvent replica.
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

# Lock exclusif : empeche deux orchestrate concurrents si le timer triggere
# avant la fin du precedent (chaine canary = ~30 min, timer = 15 min, overlap
# theoriquement possible).
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "Lock $LOCK_FILE deja pris (orchestrate en cours), skip ce cycle"
  exit 0
fi

mkdir -p "$STATE_DIR"

# Lit l'image actuellement en cache local sur CE noeud (NODE3) — c'est le
# noeud qui pilote, donc son cache reflete ce qui a ete pulle par le timer.
get_local_digest() {
  docker image inspect "${REGISTRY}/${IMAGE}:${TAG}" \
    -f '{{if .RepoDigests}}{{index .RepoDigests 0}}{{end}}' 2>/dev/null \
    | sed 's/.*@//' \
    || echo ""
}

LOCAL_DIGEST=$(get_local_digest)
if [[ -z "$LOCAL_DIGEST" ]]; then
  log "Pas d'image ${IMAGE}:${TAG} en cache local — le timer pull n'a pas encore tourne (ou registry KO). Skip."
  exit 0
fi

# Lit l'historique : <digest>\t<outcome>\t<unix_ts>
LAST_DIGEST=""
LAST_OUTCOME=""
LAST_TS=0
if [[ -f "$STATE_FILE" ]]; then
  IFS=$'\t' read -r LAST_DIGEST LAST_OUTCOME LAST_TS < "$STATE_FILE" || true
fi
NOW=$(date +%s)

# No-op : meme digest deja deploye avec succes la derniere fois.
if [[ "$LOCAL_DIGEST" == "$LAST_DIGEST" && "$LAST_OUTCOME" == "success" ]]; then
  # Pas de log par tour (idem Q6.1 doc — 95 % des tours sont des no-op).
  exit 0
fi

# Cooldown : meme digest deja tente, fail recemment.
if [[ "$LOCAL_DIGEST" == "$LAST_DIGEST" && "$LAST_OUTCOME" == "failure" ]]; then
  AGE=$((NOW - LAST_TS))
  if (( AGE < COOLDOWN_AFTER_FAIL )); then
    REMAINING=$((COOLDOWN_AFTER_FAIL - AGE))
    log "Digest $LOCAL_DIGEST a fail il y a ${AGE}s, cooldown ${REMAINING}s restant. Skip."
    exit 0
  fi
  log "Cooldown ($COOLDOWN_AFTER_FAIL s) expire, retente $LOCAL_DIGEST"
fi

log "Nouveau digest detecte : $LOCAL_DIGEST (precedent: ${LAST_DIGEST:-none})"

# GATEWAY_KEY obligatoire (alarm-cd-canary.sh refuse de tourner sans).
# Sourcable depuis /etc/alarm/cd.env via EnvironmentFile dans le unit.
if [[ -z "${GATEWAY_KEY:-}" ]]; then
  log "ERREUR : GATEWAY_KEY non definie. Creer /etc/alarm/cd.env avec GATEWAY_KEY=..."
  printf '%s\t%s\t%s\n' "$LOCAL_DIGEST" "failure" "$NOW" > "$STATE_FILE"
  exit 1
fi
export GATEWAY_KEY

# Preflight : SSH joignable vers chaque noeud cible. Defaillance ici =
# pas la peine de commencer.
log "Preflight SSH vers les 3 noeuds..."
for ip in 10.99.0.1 10.99.0.2 10.99.0.3; do
  if ! ssh -o BatchMode=yes -o ConnectTimeout=10 \
       -o StrictHostKeyChecking=accept-new \
       "alarm@$ip" "true" 2>/dev/null; then
    log "ERREUR : SSH alarm@$ip echoue (cle manquante ? noeud down ?). Halt."
    printf '%s\t%s\t%s\n' "$LOCAL_DIGEST" "failure" "$NOW" > "$STATE_FILE"
    exit 1
  fi
done
log "Preflight SSH OK pour les 3 noeuds"

# Resolution du chemin de alarm-cd-canary.sh : meme dossier que ce script.
CANARY="${SCRIPT_DIR}/alarm-cd-canary.sh"
if [[ ! -x "$CANARY" ]]; then
  log "ERREUR : $CANARY introuvable ou non executable"
  printf '%s\t%s\t%s\n' "$LOCAL_DIGEST" "failure" "$NOW" > "$STATE_FILE"
  exit 1
fi

log "Lance la chaine canary ${NODES_ORDER[*]} (soak ${SOAK}s par etape)"

# Chaine sequentielle. Halt sur premier exit code != 0.
for NODE in "${NODES_ORDER[@]}"; do
  log "--- Etape canary node${NODE} ---"
  # `--api-base` et `--gateway-key` heritage explicite pour eviter qu'un
  # changement d'env entre les etapes ne casse les POST events. GATEWAY_KEY
  # vient de /etc/alarm/cd.env via le unit systemd.
  #
  # On capture l'exit code via `cmd || rc=$?` (et non `if ! cmd; then $?`)
  # parce que `$?` apres `if !` est le code apres negation (toujours 0 sur la
  # branche fail), pas l'exit code reel du canary. set -e desactive ici par
  # `|| true` indirect via `rc=$?` — on traite l'erreur explicitement.
  rc=0
  "$CANARY" \
    --node "$NODE" \
    --soak "$SOAK" \
    --api-base "$API_BASE" \
    --gateway-key "$GATEWAY_KEY" || rc=$?
  if (( rc != 0 )); then
    log "ERREUR : canary node${NODE} a fail (exit $rc). Halt chaine."
    log "  -> les noeuds restants ${NODES_ORDER[*]} (apres node${NODE}) NE sont PAS updates."
    log "  -> consulter deployment_events pour le detail (rollback ou abort deja loggue)."
    printf '%s\t%s\t%s\n' "$LOCAL_DIGEST" "failure" "$NOW" > "$STATE_FILE"
    exit 2
  fi
  log "Etape canary node${NODE} OK"
done

log "Chaine canary complete : $LOCAL_DIGEST deploye sur les 3 noeuds"
printf '%s\t%s\t%s\n' "$LOCAL_DIGEST" "success" "$NOW" > "$STATE_FILE"
exit 0
