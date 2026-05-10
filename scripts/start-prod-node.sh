#!/bin/bash
# Wrapper de bring-up cluster Patroni en mode PROD cross-machine.
#
# Usage : ./scripts/start-prod-node.sh <1|2|3>
#   sur onsite-1 (172.16.1.121) : ./scripts/start-prod-node.sh 1
#   sur onsite-2 (172.16.1.120) : ./scripts/start-prod-node.sh 2
#   sur NODE3 cloud (10.99.0.1) : ./scripts/start-prod-node.sh 3
#
# Pourquoi ce wrapper :
# - Evite la confusion entre .env.node1 (DEV/CI mono-host) et .env.prod.node1
#   (PROD cross-machine), qui ne sont PAS interchangeables (cf docker-compose.yml
#   header et docs/PROVISIONING_ONSITE.md §22bis).
# - Verifie quelques pre-requis (file existant, ADVERTISE_HOST defini, wg0 UP)
#   avant de lancer docker compose, pour echouer tot avec un message clair plutot
#   qu'avec un timeout cryptique etcd.
#
# Cf. docs/PROVISIONING_ONSITE.md §22bis pour le contexte.

set -euo pipefail

if [[ $# -ne 1 || ! "$1" =~ ^[1-3]$ ]]; then
  echo "Usage : $0 <1|2|3>"
  echo "  1 = onsite-1 (10.99.0.2), 2 = onsite-2 (10.99.0.3), 3 = NODE3 cloud (10.99.0.1)"
  exit 1
fi
NODE="$1"

ENV_FILE=".env.prod.node${NODE}"
PROJECT="node${NODE}"

# Pre-requis 1 : le bon repertoire (clone de /opt/alarm)
if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERREUR : $ENV_FILE introuvable depuis $(pwd)"
  echo "  Cd dans /opt/alarm avant de lancer ce script."
  exit 1
fi

# Pre-requis 2 : ADVERTISE_HOST defini (= conf prod, pas mono-host par erreur)
if ! grep -q "^ADVERTISE_HOST=" "$ENV_FILE"; then
  echo "ERREUR : $ENV_FILE n'a pas ADVERTISE_HOST defini."
  echo "  Tu utilises peut-etre un .env.nodeX (CI/dev) au lieu de .env.prod.nodeX."
  echo "  Pour la prod cross-machine il FAUT .env.prod.nodeX."
  exit 1
fi

# Pre-requis 3 : interface Wireguard UP
# Sans wg0 UP, les peers etcd/patroni ne s'atteignent pas via 10.99.0.X.
# `ip -br a show wg0` sort "wg0 UNKNOWN ..." quand l'interface est up (UNKNOWN
# pour les liens point-a-point sans state link, c'est normal pour WG).
if ! ip -br a show wg0 2>/dev/null | grep -qE '\b(UP|UNKNOWN)\b'; then
  echo "ERREUR : interface wg0 n'est pas UP."
  echo "  Faire 'sudo systemctl start wg-quick@wg0' puis retenter."
  exit 1
fi

# Pre-requis 4 : .env.prod.secrets present (gitignored, depose a la main).
# Contient SECRET_KEY (signe les JWT) et eventuellement d'autres secrets a
# venir (PG passwords, FCM, SMTP). Cf .env.prod.secrets.example pour le format.
SECRETS_FILE=".env.prod.secrets"
if [[ ! -f "$SECRETS_FILE" ]]; then
  echo "ERREUR : $SECRETS_FILE manquant."
  echo "  Copie $SECRETS_FILE.example vers $SECRETS_FILE et remplis les valeurs."
  echo "  Cf docs/PROVISIONING_ONSITE.md §22quater (secrets) pour la procedure."
  exit 1
fi
# Source les secrets et les exporte vers l'env (pour la substitution \${VAR} dans
# docker-compose.yml). `set -a` exporte automatiquement chaque assignation suivante.
set -a
# shellcheck disable=SC1090
. "./$SECRETS_FILE"
set +a

# Verif anti-fail-fast : SECRET_KEY est obligatoire (sinon docker compose echouera
# en disant ":?SECRET_KEY missing", autant le dire ici avec un message clair).
if [[ -z "${SECRET_KEY:-}" ]]; then
  echo "ERREUR : SECRET_KEY n'est pas definie dans $SECRETS_FILE."
  exit 1
fi

echo "→ Bring-up cluster PROD node${NODE} avec $ENV_FILE + $SECRETS_FILE"
echo "  ADVERTISE_HOST = $(grep "^ADVERTISE_HOST=" "$ENV_FILE" | cut -d= -f2)"
echo "  SECRET_KEY     = ${SECRET_KEY:0:8}…${SECRET_KEY: -4} (longueur ${#SECRET_KEY})"
echo "  Project        = $PROJECT"
echo

exec docker compose --env-file "$ENV_FILE" -p "$PROJECT" up --build -d
