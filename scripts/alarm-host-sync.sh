#!/bin/bash
# alarm-host-sync.sh — deploie la partie hote (scripts + units infra/onsite) au commit promu :stable.
#
# Tourne en root via alarm-host-sync.timer (toutes les 5 min) sur les noeuds on-site.
# Les images Docker suivent deja :stable (alarm-cd-pull + alarm-cd-orchestrate) et le code
# gateway suit alarm-gateway:stable (alarm-gateway-sync). Les scripts et units systemd
# hote, eux, n'etaient poses qu'a la main (`sudo bash infra/onsite/install.sh`) : un correctif
# hote (ex. copie des voix par alarm-gateway-sync, 2026-10-19) n'atteignait jamais la prod.
#
# Commit cible = label org.opencontainers.image.revision de alarm-backend:stable (deja tiree
# par alarm-cd-pull) : seul un commit passe en CI + promu est deploye, comme les images.
#   1. /opt/alarm (clone git, owner alarm) -> checkout detache sur ce commit
#   2. infra/onsite/install.sh --auto (installe scripts/units, redemarre ceux qui ont change)
# Refuse un clone avec des modifications locales suivies (ne jamais les ecraser).
# Self-update : install.sh reinstalle aussi ce script et alarm-gateway-sync.
set -euo pipefail

REPO="${REPO:-/opt/alarm}"
IMAGE="${REGISTRY:-ghcr.io/claude-murgat}/alarm-backend:${TAG:-stable}"
STATE_DIR="${STATE_DIR:-/var/lib/alarm/cd}"
log() { echo "[alarm-host-sync] $*" >&2; }

SHA=$(docker image inspect "$IMAGE" -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' 2>/dev/null || true)
[[ "$SHA" =~ ^[0-9a-f]{40}$ ]] || exit 0  # image pas encore tiree, ou sans label
mkdir -p "$STATE_DIR"
[[ "$SHA" == "$(cat "$STATE_DIR/host-sha" 2>/dev/null || true)" ]] && exit 0

OWNER=$(stat -c '%U' "$REPO")
as_owner() { runuser -u "$OWNER" -- git -C "$REPO" "$@"; }
if [[ -n "$(as_owner status --porcelain --untracked-files=no)" ]]; then
  log "ERREUR : $REPO a des modifications locales suivies — deploiement hote annule"
  as_owner status --short --untracked-files=no >&2
  exit 1
fi
log "commit promu ${SHA:0:7} : mise a jour de $REPO"
as_owner fetch -q origin
as_owner checkout -q --detach "$SHA"
bash "$REPO/infra/onsite/install.sh" --auto
echo "$SHA" > "$STATE_DIR/host-sha"
log "partie hote deployee au commit ${SHA:0:7}"
