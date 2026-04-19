#!/usr/bin/env bash
# Nettoie un cluster CI lance sous un nom de projet specifique.
# A appeler en `if: always()` apres un job tier 3 pour eviter la pollution
# entre runs (volumes pgdata residuels, reseaux orphelins, etc.).
#
# Usage : ./scripts/ci-cleanup.sh <project-name>
#   project-name : nom du projet docker compose (-p), ex: ci-w1
#
# IMPORTANT : ne touche QUE au projet specifie. Ne fait PAS de prune global
# (qui casserait les autres conteneurs sur la meme machine — ex: cluster
# de dev en cours, autres projets, le runner lui-meme).

set -euo pipefail

PROJECT="${1:?usage: $0 <project-name>}"

echo "[cleanup] $PROJECT : down + remove volumes..."
docker compose -p "$PROJECT" down --volumes --remove-orphans 2>&1 || true

# Reseau dedie au projet (compose le cree avec le suffixe _default)
echo "[cleanup] $PROJECT : remove network residuel si present..."
docker network rm "${PROJECT}_default" 2>/dev/null || true

# Conteneurs orphelins matchant le prefixe (ex: si docker compose down a echoue partiellement)
orphans=$(docker ps -a --filter "name=^${PROJECT}-" --format '{{.ID}}' || true)
if [[ -n "$orphans" ]]; then
    echo "[cleanup] $PROJECT : conteneurs orphelins residuels :"
    echo "$orphans" | xargs -r docker rm -f
fi

echo "[cleanup] $PROJECT : OK"
