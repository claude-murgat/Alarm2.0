#!/bin/bash
# alarm-cd-pull.sh — pull idempotent des images GHCR :stable.
#
# Tourne via systemd timer toutes les 5 min (cf infra/onsite/systemd/alarm-cd-pull.timer).
# Compare le digest distant a celui en cache local ; pull uniquement si different.
# **Ne redemarre AUCUN container.** Le restart est l'etape canary distincte (cf PR 5).
#
# Cf docs/CD_DESIGN.md §3 (Mecanisme de pull).
#
# Sortie :
#   exit 0  -> rien a faire OU pull reussi
#   exit 1  -> erreur fatale (registry injoignable persistant, docker down, etc.)
#
# Logs : journald via `logger -t alarm-cd-pull` + stderr (visible avec systemctl status).

set -euo pipefail

REGISTRY="${REGISTRY:-ghcr.io/claude-murgat}"
TAG="${TAG:-stable}"
IMAGES=("alarm-backend" "alarm-patroni")
# /run/alarm-cd-pull/ est cree par systemd (RuntimeDirectory= dans
# alarm-cd-pull.service) avec owner alarm:alarm. Ne marche que si lance via
# systemd ou si le repertoire est pre-cree manuellement.
LOCK_FILE="/run/alarm-cd-pull/lock"
STATE_DIR="/var/lib/alarm/cd"
DIGESTS_FILE="${STATE_DIR}/digests.tsv"

log() {
  # journald + stderr (pour systemctl status).
  echo "[alarm-cd-pull] $*" >&2
  logger -t alarm-cd-pull "$*" || true
}

# Lock exclusif : evite (a) deux pulls concurrents si timer triggerise deux fois,
# (b) un pull en plein milieu d'un `start-prod-node.sh` manuel qui pourrait deja
# tirer une image en parallele. `flock -n` = non-bloquant ; si lock pris, on
# exit propre (le prochain tour du timer reprendra).
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "Lock $LOCK_FILE deja pris, skip ce cycle"
  exit 0
fi

# Pre-requis : docker daemon up.
if ! docker info >/dev/null 2>&1; then
  log "ERREUR : docker daemon non joignable"
  exit 1
fi

mkdir -p "$STATE_DIR"
touch "$DIGESTS_FILE"

# get_remote_digest : interroge le registry et retourne le digest sha256:... du
# tag distant, sans pull. `docker buildx imagetools inspect` est preferable a
# `docker manifest inspect` (qui demande experimental:true). En cas d'echec
# transient (DNS, timeout, 5xx GHCR), on retourne chaine vide et on skip cette
# image — pas d'echec fatal du script (le prochain tour retentera).
get_remote_digest() {
  local image="$1"
  docker buildx imagetools inspect "$REGISTRY/$image:$TAG" \
    --format '{{.Manifest.Digest}}' 2>/dev/null \
    || echo ""
}

# get_local_digest : digest de l'image deja pullee localement, ou chaine vide si
# l'image n'a jamais ete pullee.
#
# Le `|| echo ""` final est CRITIQUE : `docker image inspect` exit 1 si l'image
# n'existe pas localement (cas du premier pull jamais sur un noeud). Avec
# `set -euo pipefail` au top du script, la pipe entiere retournerait rc=1
# (pipefail) et le shell exit immediatement, sans aucun log applicatif.
# Symetrique avec get_remote_digest() ci-dessus.
get_local_digest() {
  local image="$1"
  docker image inspect "$REGISTRY/$image:$TAG" \
    -f '{{if .RepoDigests}}{{index .RepoDigests 0}}{{end}}' 2>/dev/null \
    | sed 's/.*@//' \
    || echo ""
}

# Boucle principale.
CHANGES=0
for IMG in "${IMAGES[@]}"; do
  REMOTE=$(get_remote_digest "$IMG")
  if [[ -z "$REMOTE" ]]; then
    log "WARN : $IMG distant inaccessible (registry timeout ou tag inexistant)"
    continue
  fi

  LOCAL=$(get_local_digest "$IMG")

  if [[ "$REMOTE" == "$LOCAL" ]]; then
    # Pas de log par tour (95% des tours sont des no-op, cf doc Q6.1).
    continue
  fi

  log "$IMG : digest different (local=${LOCAL:-none} remote=$REMOTE), pull..."
  if ! docker pull "$REGISTRY/$IMG:$TAG"; then
    log "ERREUR : docker pull $IMG a echoue"
    continue
  fi

  # Journal append-only des digests, K=5 derniers conserves dans cleanup nightly
  # separe (pas dans ce script — keep it simple). Format TSV pour facilite de
  # parsing par PR 5/6 (orchestrateur canary qui lit l'historique pour rollback).
  printf '%s\t%s\t%s\t%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    "$IMG" \
    "$REMOTE" \
    "${LOCAL:-none}" \
    >> "$DIGESTS_FILE"

  CHANGES=$((CHANGES + 1))
  log "$IMG : pull OK"
done

if [[ "$CHANGES" -gt 0 ]]; then
  log "Pull cycle complet : $CHANGES image(s) mises a jour"
fi

exit 0
