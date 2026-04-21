#!/usr/bin/env bash
# Wrapper pour gerer la cle privee multi-lignes (limite docker compose .env).
# Usage : ./infra/runner/runner.sh [up|down|logs|ps|restart]

set -euo pipefail

cd "$(dirname "$0")"

ACTION="${1:-up}"
KEY_FILE="alarm-bot.private-key.pem"
ENV_FILE=".env"

if [[ ! -f "$KEY_FILE" ]]; then
    echo "ERROR: $KEY_FILE introuvable dans infra/runner/" >&2
    echo "Telecharge la cle privee depuis la GitHub App alarm-murgat-bot." >&2
    exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: $ENV_FILE introuvable. Lance : cp .env.example .env" >&2
    exit 1
fi

# Lit la cle .pem dans une variable d'env (multi-lignes preservees).
export APP_PRIVATE_KEY="$(cat "$KEY_FILE")"

case "$ACTION" in
    up)
        docker compose -f docker-compose.runner.yml --env-file "$ENV_FILE" up -d --scale gh-runner=4
        echo ""
        echo "Runner lance. Verifie qu'il apparait Idle :"
        echo "  https://github.com/$(grep GH_OWNER "$ENV_FILE" | cut -d= -f2)/$(grep GH_REPO "$ENV_FILE" | cut -d= -f2)/settings/actions/runners"
        ;;
    down)
        docker compose -f docker-compose.runner.yml --env-file "$ENV_FILE" down
        ;;
    logs)
        docker compose -f docker-compose.runner.yml --env-file "$ENV_FILE" logs -f --tail=50
        ;;
    ps)
        docker compose -f docker-compose.runner.yml --env-file "$ENV_FILE" ps
        ;;
    restart)
        docker compose -f docker-compose.runner.yml --env-file "$ENV_FILE" restart
        ;;
    *)
        echo "Usage: $0 [up|down|logs|ps|restart]" >&2
        exit 1
        ;;
esac
