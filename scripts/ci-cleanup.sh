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
#
# Robustesse (CI-BUG-09, 2026-04-20) :
# - Chaque operation timeoute dur (30s max) pour eviter qu'un container
#   coince bloque le job jusqu'a son timeout global.
# - Apres cleanup, on verifie l'etat residuel. S'il reste quelque chose,
#   on force-kill et on emet une annotation GitHub Actions ::error::
#   pour que le probleme soit visible dans les logs du run suivant.

set -uo pipefail  # PAS -e : on veut continuer meme si une commande timeout

PROJECT="${1:?usage: $0 <project-name>}"
HAD_RESIDUAL=0

# --- Etape 1 : compose down -v --remove-orphans (timeout 30s)
echo "[cleanup] $PROJECT : compose down -v --remove-orphans (timeout 30s)..."
if ! timeout 30 docker compose -p "$PROJECT" down --volumes --remove-orphans --timeout 10 2>&1; then
    echo "::warning file=scripts/ci-cleanup.sh::compose down timeout/failed for $PROJECT — falling back to force-remove"
    HAD_RESIDUAL=1
fi

# --- Etape 2 : reseau (compose le cree avec suffixe _default)
echo "[cleanup] $PROJECT : remove network ${PROJECT}_default si present..."
timeout 10 docker network rm "${PROJECT}_default" 2>/dev/null || true

# --- Etape 3 : state check + force-cleanup des residus
echo "[cleanup] $PROJECT : verification etat residuel..."

LEFTOVER_CONTAINERS=$(docker ps -a --filter "name=^${PROJECT}-" --format '{{.ID}}' 2>/dev/null || true)
LEFTOVER_VOLUMES=$(docker volume ls --filter "name=^${PROJECT}_" --format '{{.Name}}' 2>/dev/null || true)
LEFTOVER_NETWORKS=$(docker network ls --filter "name=^${PROJECT}_" --format '{{.Name}}' 2>/dev/null || true)

if [[ -n "$LEFTOVER_CONTAINERS" ]]; then
    echo "::warning file=scripts/ci-cleanup.sh::residual containers for $PROJECT :"
    echo "$LEFTOVER_CONTAINERS"
    echo "$LEFTOVER_CONTAINERS" | xargs -r timeout 20 docker rm -f 2>&1 || true
    HAD_RESIDUAL=1
fi

if [[ -n "$LEFTOVER_VOLUMES" ]]; then
    echo "::warning file=scripts/ci-cleanup.sh::residual volumes for $PROJECT :"
    echo "$LEFTOVER_VOLUMES"
    echo "$LEFTOVER_VOLUMES" | xargs -r timeout 15 docker volume rm -f 2>&1 || true
    HAD_RESIDUAL=1
fi

if [[ -n "$LEFTOVER_NETWORKS" ]]; then
    echo "::warning file=scripts/ci-cleanup.sh::residual networks for $PROJECT :"
    echo "$LEFTOVER_NETWORKS"
    echo "$LEFTOVER_NETWORKS" | xargs -r timeout 10 docker network rm 2>&1 || true
    HAD_RESIDUAL=1
fi

# --- Etape 4 : check final apres force-cleanup
FINAL_CHECK=$(
    docker ps -a --filter "name=^${PROJECT}-" --format '{{.Names}}' 2>/dev/null
    docker volume ls --filter "name=^${PROJECT}_" --format '{{.Name}}' 2>/dev/null
    docker network ls --filter "name=^${PROJECT}_" --format '{{.Name}}' 2>/dev/null
)

if [[ -n "$FINAL_CHECK" ]]; then
    echo "::error file=scripts/ci-cleanup.sh::CLEANUP INCOMPLETE for $PROJECT — leftover:"
    echo "$FINAL_CHECK"
    echo "[cleanup] $PROJECT : ECHEC — etat residuel persiste, intervention manuelle requise."
    exit 2
fi

if [[ $HAD_RESIDUAL -eq 1 ]]; then
    echo "[cleanup] $PROJECT : OK (avec force-cleanup de residus — voir warnings)"
else
    echo "[cleanup] $PROJECT : OK (clean)"
fi
