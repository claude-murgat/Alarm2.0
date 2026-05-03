#!/usr/bin/env bash
# Regenere .test_durations pour le load-balancing pytest-split en tier 3 CI.
#
# Pourquoi :
#   pytest-split (pr.yml tier3 : `--splits 2 --group N`) utilise `.test_durations`
#   a la racine du repo pour equilibrer les workers par DUREE (algo greedy).
#   Sans ce fichier, le split tombe sur l'ordre alphabetique avec poids 1.0/test
#   -> count-based, donc desequilibre quand un fichier a des sleeps lourds
#   (ex: test_sms_voice.py avec TICK_WAIT=22s * 21 occurrences = 8m30s de sleep
#   pur, tombe a 100% sur worker 2 -> asymetrie 7m vs 14m observee).
#
# Avec un fichier .test_durations a jour, pytest-split equilibre par temps reel
# et l'asymetrie disparait.
#
# Quand re-generer :
#   - Apres ajout d'un test substantiel (> 30s wall-clock)
#   - Apres refactor qui change significativement les durees
#     (ex: sleep aveugle -> /test/advance-clock)
#   - Trimestre / quand l'asymetrie worker1 vs worker2 revient
#
# Prerequis :
#   - Cluster up : docker compose --env-file .env.dev -p dev up --build -d
#     (single node dev suffit, c'est juste pour faire passer les tests)
#   - BACKEND_URL pointe vers le backend (defaut http://localhost:8000)
#   - Toutes les deps : pip install -r backend/requirements.txt -r requirements-dev.txt
#
# Usage :
#   ./scripts/regenerate-test-durations.sh
#
# Estimation : ~15-20 min (somme des 2 workers tier 3 actuels, tout en serie).
#
# Apres execution :
#   git checkout -b ci/refresh-test-durations
#   git add .test_durations
#   git commit -m "ci: refresh test durations for pytest-split balancing"
#   git push -u origin ci/refresh-test-durations
#   gh pr create

set -euo pipefail

BACKEND_URL="${BACKEND_URL:-http://localhost:8000}"

# Sanity : un cluster doit etre up sinon les tests E2E vont fail/skip
if ! curl -sf "$BACKEND_URL/health" > /dev/null 2>&1; then
    echo "::error::Backend non accessible sur $BACKEND_URL"
    echo "Demarrer un cluster avant :"
    echo "  COMPOSE_PROFILES=mailhog docker compose --env-file .env.dev -p dev up --build -d"
    exit 1
fi

echo "[regen-durations] Backend OK sur $BACKEND_URL"
echo "[regen-durations] Lancement pytest avec --store-durations..."
echo "[regen-durations] Estimation : ~15-20 min (somme worker1 + worker2 tier 3 actuels)"
echo

# Meme commande que tier3 dans pr.yml MAIS :
#   - sans --splits / --group : on veut l'integralite des durees en un seul run
#   - avec --store-durations : ecrit .test_durations a la racine du repo
#   - sans --junitxml : pas besoin pour ce one-shot
pytest tests/ \
    --ignore=tests/unit \
    --ignore=tests/integration \
    --ignore=tests/test_frontend.py \
    --ignore=tests/test_failback.py \
    --ignore=tests/test_manual_multi_emulator.py \
    --ignore=tests/test_dtmf_decoder.py \
    --skip-failover \
    -p no:randomly \
    --store-durations

echo
echo "[regen-durations] OK : .test_durations a la racine du repo."
echo
echo "Etapes suivantes :"
echo "  git checkout -b ci/refresh-test-durations"
echo "  git add .test_durations"
echo "  git commit -m 'ci: refresh test durations for pytest-split balancing'"
echo "  git push -u origin ci/refresh-test-durations"
echo "  gh pr create --title 'ci: refresh test durations'"
