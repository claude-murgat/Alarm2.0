#!/bin/bash
# alarm-gateway-sync.sh — deploie le code gateway promu en :stable.
#
# Tourne en root via alarm-gateway-sync.timer (toutes les 5 min) sur les 3 noeuds.
# Ne fait rien sur un noeud sans gateway (pas d'unit alarm-sms-gateway).
# L'image est tiree par alarm-cd-pull ; ce script ne touche pas au registry.
#
# Quand le digest local de alarm-gateway:stable change :
#   1. extrait /gateway/*.py et /gateway/voice/*.wav de l'image
#   2. si requirements.txt a change : pip install (sinon le nouveau code
#      demarrerait sans ses dependances)
#   3. sauvegarde l'ancien code, installe le nouveau, redemarre la gateway
#   4. attend "Gateway operationnelle" ; sinon restaure l'ancien code
#
# Ferme le gap de l'issue #183 (code gateway jamais redeploye automatiquement).
# exit 0 : rien a faire, deploye, ou rollback reussi (erreur loggee)
# exit 1 : erreur fatale (rollback impossible, docker down...)

set -euo pipefail

REGISTRY="${REGISTRY:-ghcr.io/claude-murgat}"
IMAGE="${REGISTRY}/alarm-gateway:${TAG:-stable}"
GW_DIR="${GW_DIR:-/opt/alarm-gateway}"
GW_UNIT="${GW_UNIT:-alarm-sms-gateway.service}"
STATE_DIR="${STATE_DIR:-/var/lib/alarm/cd}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-180}"   # detection modem ~45 s observee

log() { echo "[alarm-gateway-sync] $*" >&2; }

if [[ ! -f "/etc/systemd/system/$GW_UNIT" ]]; then
  exit 0  # noeud sans gateway
fi

ID=$(docker image inspect "$IMAGE" -f '{{.Id}}' 2>/dev/null || echo "")
if [[ -z "$ID" ]]; then
  exit 0  # image pas encore tiree par alarm-cd-pull
fi

mkdir -p "$STATE_DIR"
if [[ "$ID" == "$(cat "$STATE_DIR/gateway-image-id" 2>/dev/null || true)" ]]; then
  exit 0
fi

NEW=$(mktemp -d)
trap 'rm -rf "$NEW"' EXIT
CID=$(docker create "$IMAGE")
docker cp "$CID:/gateway/." "$NEW/"
docker rm "$CID" >/dev/null

CHANGED=""
# voice/*.wav : messages vocaux des appels (INV-131/132)
for f in "$NEW"/*.py "$NEW"/voice/*.wav; do
  [[ -e "$f" ]] || continue
  rel="${f#"$NEW"/}"
  cmp -s "$f" "$GW_DIR/$rel" || CHANGED="$CHANGED $rel"
done
if [[ -z "$CHANGED" ]]; then
  echo "$ID" > "$STATE_DIR/gateway-image-id"
  exit 0
fi

log "nouveau code gateway (image $ID), fichiers modifies :$CHANGED"

if ! cmp -s "$NEW/requirements.txt" "$STATE_DIR/gateway-requirements.txt"; then
  log "requirements.txt change : pip install"
  if ! python3 -m pip install --quiet --break-system-packages -r "$NEW/requirements.txt"; then
    log "ERREUR : pip install a echoue, deploiement annule (ancien code conserve)"
    exit 1
  fi
  cp "$NEW/requirements.txt" "$STATE_DIR/gateway-requirements.txt"
fi

OWNER=$(stat -c '%U:%G' "$GW_DIR")
PREV="$STATE_DIR/gateway-prev"
rm -rf "$PREV" && mkdir -p "$PREV"
cp -p "$GW_DIR"/*.py "$PREV/"
if compgen -G "$GW_DIR/voice/*.wav" >/dev/null; then
  mkdir -p "$PREV/voice" && cp -p "$GW_DIR"/voice/*.wav "$PREV/voice/"
fi

install_and_check() {
  local src="$1" since
  install -o "${OWNER%:*}" -g "${OWNER#*:}" -m 644 "$src"/*.py "$GW_DIR/"
  if compgen -G "$src/voice/*.wav" >/dev/null; then
    install -d -o "${OWNER%:*}" -g "${OWNER#*:}" -m 755 "$GW_DIR/voice"
    install -o "${OWNER%:*}" -g "${OWNER#*:}" -m 644 "$src"/voice/*.wav "$GW_DIR/voice/"
  fi
  since=$(date '+%Y-%m-%d %H:%M:%S')
  systemctl restart "$GW_UNIT"
  for _ in $(seq 1 $((HEALTH_TIMEOUT / 5))); do
    sleep 5
    journalctl -u "$GW_UNIT" --since "$since" --no-pager 2>/dev/null \
      | grep -q "Gateway operationnelle" && return 0
  done
  return 1
}

if install_and_check "$NEW"; then
  echo "$ID" > "$STATE_DIR/gateway-image-id"
  log "deploiement OK, gateway operationnelle"
  exit 0
fi

log "ERREUR : gateway non operationnelle apres ${HEALTH_TIMEOUT}s, restauration de l'ancien code"
if install_and_check "$PREV"; then
  # Image marquee comme traitee : pas de boucle deploiement/rollback toutes les 5 min.
  echo "$ID" > "$STATE_DIR/gateway-image-id"
  log "rollback OK, ancien code operationnel ; nouvelle version NON deployee"
  exit 0
fi
log "ERREUR FATALE : l'ancien code ne redemarre pas non plus"
exit 1
