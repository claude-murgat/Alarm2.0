#!/usr/bin/env bash
# Polling healthcheck pour cluster Patroni + backends, utilise par tier 3.
# Remplace les `sleep N` magiques : on attend la condition, pas une duree.
#
# Usage : ./scripts/ci-wait-cluster.sh <project-name> [timeout-seconds]
#   project-name   : nom du projet docker compose (-p), ex: ci-w1
#   timeout        : defaut 120s

set -euo pipefail

PROJECT="${1:?usage: $0 <project-name> [timeout-seconds]}"
TIMEOUT="${2:-120}"
INTERVAL=2

deadline=$(($(date +%s) + TIMEOUT))

# 1. Tous les conteneurs du projet sont up
echo "[wait-cluster] $PROJECT : conteneurs up..."
while [[ $(date +%s) -lt $deadline ]]; do
    if docker compose -p "$PROJECT" ps --format '{{.State}}' | grep -qv 'running'; then
        sleep $INTERVAL
        continue
    fi
    [[ -n "$(docker compose -p "$PROJECT" ps -q)" ]] && break
    sleep $INTERVAL
done

# 2. Patroni a elu un leader (REST API repond + role=master OU primary)
echo "[wait-cluster] $PROJECT : leader Patroni elu..."
PATRONI_PORT="${PATRONI_PORT:-8008}"
while [[ $(date +%s) -lt $deadline ]]; do
    role=$(docker compose -p "$PROJECT" exec -T patroni curl -sf "http://localhost:8008/" 2>/dev/null \
           | python -c "import json,sys; print(json.load(sys.stdin).get('role',''))" 2>/dev/null || true)
    if [[ "$role" == "master" || "$role" == "primary" ]]; then
        echo "[wait-cluster] $PROJECT : Patroni leader actif (role=$role)"
        break
    fi
    sleep $INTERVAL
done

# 3. Backend repond depuis le host (image python:slim n'a pas curl, on check de l'exterieur)
url="${BACKEND_URL:-http://localhost:8000}"
echo "[wait-cluster] $PROJECT : backend up sur $url ..."
while [[ $(date +%s) -lt $deadline ]]; do
    if curl -sf "$url/" >/dev/null 2>&1 || curl -sf "$url/health" >/dev/null 2>&1; then
        echo "[wait-cluster] $PROJECT : backend repond"
        echo "[wait-cluster] $PROJECT : OK (cluster pret en $(($(date +%s) - (deadline - TIMEOUT)))s)"
        exit 0
    fi
    sleep $INTERVAL
done

echo "[wait-cluster] $PROJECT : TIMEOUT apres ${TIMEOUT}s" >&2
docker compose -p "$PROJECT" ps >&2
docker compose -p "$PROJECT" logs --tail=30 >&2
exit 1
