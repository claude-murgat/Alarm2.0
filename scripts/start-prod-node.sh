#!/bin/bash
# Wrapper de bring-up cluster Patroni en mode PROD cross-machine.
#
# Usage : ./scripts/start-prod-node.sh <1|2|3|4|5>
#   sur onsite-1 (192.168.1.50) : ./scripts/start-prod-node.sh 1
#   sur onsite-2 (192.168.1.51) : ./scripts/start-prod-node.sh 2
#   sur NODE3 cloud (10.99.0.1) : ./scripts/start-prod-node.sh 3
#   sur alarm-1 (10.99.0.4)     : ./scripts/start-prod-node.sh 4   (non votant)
#   sur alarm-2 (10.99.0.5)     : ./scripts/start-prod-node.sh 5   (non votant)
#
# Nœuds 4/5 NON VOTANTS etcd : Patroni + backend seulement, AUCUN conteneur etcd
# local (`up --no-deps patroni backend`). Ils utilisent les 3 etcd votants
# (10.99.0.1/.2/.3) via WG, cf ETCD_HOSTS de .env.prod.node4/5.
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

# Defaults : mode "build local" pour compatibilite retro (avant PR CD V1).
MODE="build"

# Parse arguments. Ordre flexible : `--pull <N>` ou `<N> --pull`.
for arg in "$@"; do
  case "$arg" in
    --pull)
      MODE="pull"
      ;;
    --build)
      MODE="build"  # explicite, meme si c'est le defaut
      ;;
    [1-5])
      NODE="$arg"
      ;;
    *)
      echo "Argument inconnu : $arg"
      echo "Usage : $0 [--pull|--build] <1|2|3|4|5>"
      echo "  1 = onsite-1 (10.99.0.2), 2 = onsite-2 (10.99.0.3), 3 = NODE3 cloud (10.99.0.1)"
      echo "  4 = alarm-1 (10.99.0.4), 5 = alarm-2 (10.99.0.5) : non votants, sans etcd local"
      echo "  --pull  : pull l'image depuis GHCR (PROD V1+, cf docs/CD_DESIGN.md §3)"
      echo "  --build : build l'image localement (defaut, retro-compat avant CD V1)"
      exit 1
      ;;
  esac
done

if [[ -z "${NODE:-}" || ! "$NODE" =~ ^[1-5]$ ]]; then
  echo "Usage : $0 [--pull|--build] <1|2|3|4|5>"
  exit 1
fi

ENV_FILE=".env.prod.node${NODE}"
PROJECT="node${NODE}"

# Votants 1-3 : tous les services (etcd inclus), commande inchangee (tableaux vides).
# Non votants 4/5 : Patroni + backend seuls, --no-deps -> jamais de conteneur etcd.
SERVICES=()
NO_DEPS=()
if [[ "$NODE" =~ ^[45]$ ]]; then
  SERVICES=(patroni backend)
  NO_DEPS=(--no-deps)
fi

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

# Pre-requis 2bis : INV-125 — refuser le bring-up si etcd (2379/2380), PostgreSQL
# (5432) et Patroni REST (8008) seraient publies sur une IP publiquement
# routable. Ces trois surfaces sont non authentifiees, et UFW n'y change rien :
# les regles iptables de Docker le court-circuitent, un port publie reste
# joignable depuis Internet alors que `ufw status` l'affiche ferme.
#
# Le pre-requis 2 ci-dessus ne verifie que la PRESENCE de la cle ; il passe pour
# `ADVERTISE_HOST=` (vide) ou `ADVERTISE_HOST=0.0.0.0`, qui rebindent tout sur
# 0.0.0.0 en silence. Sur une machine sans IP publique (onsite derriere NAT,
# CI/dev mono-host) le check est un no-op.
if ! command -v python3 >/dev/null 2>&1; then
  echo "ERREUR : python3 absent — exposition reseau non verifiable (INV-125)."
  echo "  Installer python3, ou verifier a la main que ADVERTISE_HOST pointe sur"
  echo "  l'IP WireGuard du nœud avant de lancer docker compose."
  exit 1
fi
if ! python3 "$(dirname "$0")/check-bind-exposure.py" --env-file "$ENV_FILE"; then
  echo "Bring-up annule : corriger ADVERTISE_HOST dans $ENV_FILE puis retenter."
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
echo "  Mode           = $MODE"
echo

if [[ "$MODE" == "pull" ]]; then
  # Mode prod V1+ : pull avant up. Necessite PR 1 (workflow cd-build) mergee
  # pour que les images existent sur GHCR. Verifier que REGISTRY et IMAGE_TAG
  # sont definis dans .env.prod.nodeN (PR 2 du plan §8).
  if ! grep -q "^REGISTRY=" "$ENV_FILE" || ! grep -q "^IMAGE_TAG=" "$ENV_FILE"; then
    echo "ERREUR : $ENV_FILE n'a pas REGISTRY et IMAGE_TAG definis."
    echo "  Ces variables sont requises en mode --pull (cf PR 2 du plan CD V1)."
    echo "  Ajouter en fin de fichier :"
    echo "    REGISTRY=ghcr.io/claude-murgat"
    echo "    IMAGE_TAG=stable"
    exit 1
  fi
  echo "→ docker compose pull ${SERVICES[*]}..."
  docker compose --env-file "$ENV_FILE" -p "$PROJECT" pull "${SERVICES[@]}"
  echo "→ docker compose up -d ${NO_DEPS[*]} ${SERVICES[*]} (sans --build)..."
  exec docker compose --env-file "$ENV_FILE" -p "$PROJECT" up -d "${NO_DEPS[@]}" "${SERVICES[@]}"
else
  # Mode dev/CI ou retro-compat : build local.
  exec docker compose --env-file "$ENV_FILE" -p "$PROJECT" up --build -d "${NO_DEPS[@]}" "${SERVICES[@]}"
fi
