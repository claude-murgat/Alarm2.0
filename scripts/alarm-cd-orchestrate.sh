#!/bin/bash
# alarm-cd-orchestrate.sh — chaine canary 3 -> 5 -> 4 -> 2 -> 1 automatique (V1.5).
#
# Cf docs/CD_DESIGN.md §4 (ordre canary) et §8 (plan PR 9 — "promotion canary
# auto apres soak vert"). Wraps alarm-cd-canary.sh pour les 5 noeuds en sequence,
# halt immediat sur le premier echec (l'auto-rollback du noeud cible est gere
# par alarm-cd-canary.sh -> alarm-cd-rollback.sh).
#
# Tourne via timer systemd `alarm-cd-orchestrate.timer` sur NODE3 cloud
# (le seul noeud qui voit les 5 cibles via le mesh WG). Le timer est livre
# **disable** par defaut — l'operateur l'active explicitement apres avoir
# valide 5 cycles canary manuels (cf §4 doc "V1.5 a activer apres 5
# deploiements clean").
#
# Idempotence + deploiement independant par site (INV-167, incident 2026-10-29) :
#   - Etat par noeud : /var/lib/alarm/cd/node<N>.digest = digest deploye avec
#     succes sur node<N> (ecrit apres son canary OK). Un noeud deja au digest
#     courant de :stable (cache local NODE3) est saute.
#   - Les 5 noeuds au digest courant -> no-op silencieux (95 % des ticks).
#   - Noeud injoignable (SSH) -> WARN, reporte au prochain passage, la chaine
#     CONTINUE avec les suivants. Un votant etcd (1, 2, 3) n'est redemarre que
#     si les 3 votants repondent (quorum) ; sinon seuls 4/5 sont deployes.
#   - Etat global /var/lib/alarm/cd/orchestrate.state : <digest>\t<outcome>\t<ts>,
#     outcome = success (5 noeuds a jour) | partial (noeuds en attente, repris
#     au tick suivant, SANS cooldown) | failure (un canary a fail).
#   - Meme digest + outcome=failure + cooldown < 1 h -> skip (anti-boucle si
#     :stable est casse).
#
# Pre-requis (cf docs/CD_DESIGN.md §3 + §4) :
#   - alarm-cd-pull.timer actif sur les 5 noeuds (pour que les images soient
#     en cache local AVANT l'orchestration).
#   - Cle SSH alarm@10.99.0.{1,2,3,4,5} fonctionnelle depuis NODE3 (un noeud
#     injoignable est reporte, il ne bloque plus les autres).
#   - GATEWAY_KEY definie via /etc/alarm/cd.env (lu par le unit systemd).
#
# Sortie :
#   exit 0  -> rien a faire, cooldown en cours, chaine complete (success) OU
#              partielle (partial : noeuds injoignables reportes au tick suivant)
#   exit 1  -> erreur de config (GATEWAY_KEY manquante, canary introuvable)
#   exit 2  -> chaine canary halt (un noeud a fail/rollback) — l'event est deja
#              loggue par alarm-cd-canary.sh / alarm-cd-rollback.sh

set -euo pipefail

REGISTRY="${REGISTRY:-ghcr.io/claude-murgat}"
TAG="${TAG:-stable}"
IMAGE="${IMAGE:-alarm-backend}"
# STATE_DIR / LOCK_FILE surchargeables pour les tests (tests/unit/test_cd_orchestrate.py).
STATE_DIR="${STATE_DIR:-/var/lib/alarm/cd}"
STATE_FILE="${STATE_DIR}/orchestrate.state"
# Verrou dans STATE_DIR (cree par systemd StateDirectory=, owner alarm) et non
# dans /run/ : RuntimeDirectory= disparait a la fin du oneshot, or alarm-chaos.sh
# (INV-178) prend le meme verrou pour ne jamais tester pendant un deploiement.
LOCK_FILE="${LOCK_FILE:-${STATE_DIR}/orchestrate.lock}"
API_BASE_DEFAULT="http://10.99.0.1:8000"
API_BASE="${API_BASE:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Cooldown apres echec (rollback) : 1 h, anti-boucle si :stable cassé en boucle.
# Cf §5 doc — "hystérésis : pas de nouvelle tentative de pull :stable sur ce
# noeud pendant 1 h". Ici on porte ça au niveau orchestration (chaine entiere).
COOLDOWN_AFTER_FAIL=3600
# Soak entre etapes (override possible via env). 120 s depuis le 2026-10-20 (choix proprietaire :
# ~10 min de deploiement au lieu de ~35) ; toujours un noeud a la fois, rollback auto conserve.
SOAK="${SOAK:-120}"
# Ordre canary fige : NODE3 (cloud, le moins critique) -> node5 alarm-2 puis
# node4 alarm-1 (nouveaux, non votants etcd : un echec n'entame pas le quorum)
# -> onsite-2 (Celeron, maillon faible) -> onsite-1 (i5, le plus puissant et
# souvent primary). node1 reste le dernier (son canary_promoted = fin de chaine,
# cf backend/app/api/deployments.py). Cf §4 doc — justification de l'ordre.
NODES_ORDER=(3 5 4 2 1)
# IP WG (cf alarm-cd-canary.sh) et site (logs seulement). Votants etcd = 1, 2, 3.
NODE_IP=([1]=10.99.0.2 [2]=10.99.0.3 [3]=10.99.0.1 [4]=10.99.0.4 [5]=10.99.0.5)
NODE_SITE=([1]=Izeron [2]=Izeron [3]=OVH [4]=Beaufort [5]=Beaufort)

log() {
  # stderr seul -> systemd capture via StandardError=journal (cf unit file).
  echo "[alarm-cd-orchestrate] $*" >&2
}

# Decouvre le leader Patroni via GET /health role=primary sur les 5 IPs WG.
# POST /api/deployments/events (par canary) requiert le leader (replicas -> 503).
# Sans ce bloc, --api-base default = 10.99.0.1 = NODE3 souvent replica.
discover_leader() {
  local ip role
  for ip in 10.99.0.1 10.99.0.2 10.99.0.3 10.99.0.4 10.99.0.5; do
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

# Noeuds a deployer = ceux dont node<N>.digest != digest courant. Un noeud
# revenu apres une absence est donc rattrape au tick suivant, meme si le
# digest n'a pas change depuis.
TODO=()
for NODE in "${NODES_ORDER[@]}"; do
  if [[ "$(cat "${STATE_DIR}/node${NODE}.digest" 2>/dev/null || true)" != "$LOCAL_DIGEST" ]]; then
    TODO+=("$NODE")
  fi
done
is_todo() { [[ " ${TODO[*]} " == *" $1 "* ]]; }

# No-op : les 5 noeuds sont deja au digest courant.
if (( ${#TODO[@]} == 0 )); then
  # Pas de log par tour (idem Q6.1 doc — 95 % des tours sont des no-op).
  exit 0
fi

# Cooldown : meme digest deja tente, un canary a fail recemment. Un passage
# partial (noeuds injoignables) ne declenche PAS de cooldown.
if [[ "$LOCAL_DIGEST" == "$LAST_DIGEST" && "$LAST_OUTCOME" == "failure" ]]; then
  AGE=$((NOW - LAST_TS))
  if (( AGE < COOLDOWN_AFTER_FAIL )); then
    REMAINING=$((COOLDOWN_AFTER_FAIL - AGE))
    log "Digest $LOCAL_DIGEST a fail il y a ${AGE}s, cooldown ${REMAINING}s restant. Skip."
    exit 0
  fi
  log "Cooldown ($COOLDOWN_AFTER_FAIL s) expire, retente $LOCAL_DIGEST"
fi

log "Digest $LOCAL_DIGEST (precedent: ${LAST_DIGEST:-none}${LAST_OUTCOME:+ $LAST_OUTCOME}) — noeuds a deployer : ${TODO[*]}"

# GATEWAY_KEY obligatoire (alarm-cd-canary.sh refuse de tourner sans).
# Sourcable depuis /etc/alarm/cd.env via EnvironmentFile dans le unit.
if [[ -z "${GATEWAY_KEY:-}" ]]; then
  log "ERREUR : GATEWAY_KEY non definie. Creer /etc/alarm/cd.env avec GATEWAY_KEY=..."
  printf '%s\t%s\t%s\n' "$LOCAL_DIGEST" "failure" "$NOW" > "$STATE_FILE"
  exit 1
fi
export GATEWAY_KEY

# Preflight SSH : les noeuds a deployer, plus les 3 votants des que l'un d'eux
# est a deployer. Un noeud injoignable n'arrete plus la chaine : jusqu'au
# 2026-10-29, node4/5 hors ligne 2 jours -> exit 1 a chaque tour, aucune des 4
# versions promues n'a ete deployee, sur aucun site.
DOWN=" "
VOTER_TODO=0
if is_todo 1 || is_todo 2 || is_todo 3; then VOTER_TODO=1; fi
for NODE in "${NODES_ORDER[@]}"; do
  if ! is_todo "$NODE" && ! (( VOTER_TODO && NODE <= 3 )); then
    continue
  fi
  if ! ssh -o BatchMode=yes -o ConnectTimeout=10 \
       -o StrictHostKeyChecking=accept-new \
       "alarm@${NODE_IP[$NODE]}" "true" 2>/dev/null; then
    DOWN+="$NODE "
  fi
done
# Quorum etcd (INV-090) : redemarrer un votant pendant qu'un autre est absent
# ne laisserait qu'un etcd sur 3 -> quorum perdu. Les 3 votants ou aucun.
VOTERS_OK=1
for NODE in 1 2 3; do
  if [[ "$DOWN" == *" $NODE "* ]]; then VOTERS_OK=0; fi
done
if [[ "$DOWN" == " " ]]; then
  log "Preflight SSH OK"
else
  log "Preflight SSH : injoignables :${DOWN% }"
fi
if (( VOTER_TODO && ! VOTERS_OK )); then
  log "WARN : votant etcd injoignable : aucun votant (1, 2, 3) redemarre ce passage (quorum etcd), seuls les non votants 4/5 sont deployes"
fi

# Resolution du chemin de alarm-cd-canary.sh : meme dossier que ce script.
CANARY="${SCRIPT_DIR}/alarm-cd-canary.sh"
if [[ ! -x "$CANARY" ]]; then
  log "ERREUR : $CANARY introuvable ou non executable"
  printf '%s\t%s\t%s\n' "$LOCAL_DIGEST" "failure" "$NOW" > "$STATE_FILE"
  exit 1
fi

log "Lance la chaine canary ${TODO[*]} (soak ${SOAK}s par etape)"

# Chaine sequentielle. Noeud injoignable -> reporte (PENDING), on continue.
# Canary en echec -> halt (une image cassee ne doit pas se propager).
PENDING=()
for NODE in "${TODO[@]}"; do
  if [[ "$DOWN" == *" $NODE "* ]]; then
    log "WARN : node${NODE} (${NODE_SITE[$NODE]}) injoignable : reporte au prochain passage"
    PENDING+=("$NODE")
    continue
  fi
  if (( NODE <= 3 && ! VOTERS_OK )); then
    log "WARN : node${NODE} (${NODE_SITE[$NODE]}, votant) reporte au prochain passage : un votant etcd est injoignable"
    PENDING+=("$NODE")
    continue
  fi
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
    log "  -> les noeuds restants de ${TODO[*]} (apres node${NODE}) NE sont PAS updates."
    log "  -> consulter deployment_events pour le detail (rollback ou abort deja loggue)."
    printf '%s\t%s\t%s\n' "$LOCAL_DIGEST" "failure" "$NOW" > "$STATE_FILE"
    exit 2
  fi
  printf '%s\n' "$LOCAL_DIGEST" > "${STATE_DIR}/node${NODE}.digest"
  log "Etape canary node${NODE} OK"
done

if (( ${#PENDING[@]} > 0 )); then
  log "Chaine canary partielle : $LOCAL_DIGEST deploye, en attente : ${PENDING[*]} (repris au prochain passage, sans cooldown)"
  printf '%s\t%s\t%s\n' "$LOCAL_DIGEST" "partial" "$NOW" > "$STATE_FILE"
  exit 0
fi
log "Chaine canary complete : $LOCAL_DIGEST deploye sur les 5 noeuds"
printf '%s\t%s\t%s\n' "$LOCAL_DIGEST" "success" "$NOW" > "$STATE_FILE"
exit 0
