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
# Modifications locales suivies : acceptees seulement si le commit cible les contient deja
# (rien n'est perdu) ; sinon refus, rien n'est ecrase.
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

# Chaque bloc ajoute localement (base -> local) existe tel quel dans la cible ; une suppression
# pure ne doit plus y figurer. Repli quand merge-file voit un conflit entre lignes voisines
# (la cible a aussi modifie le voisinage) alors que rien ne serait perdu.
blocks_in_target() {
  python3 - "$1" "$2" "$3" <<'PY'
import difflib, sys
base, ours, theirs = (open(p, encoding="utf-8", errors="replace").read().splitlines() for p in sys.argv[1:4])
def has(block):
    n = len(block)
    return any(theirs[i:i + n] == block for i in range(len(theirs) - n + 1))
for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, base, ours, autojunk=False).get_opcodes():
    if op == "equal":
        continue
    added, removed = ours[j1:j2], base[i1:i2]
    if (added and not has(added)) or (not added and has(removed)):
        sys.exit(1)
PY
}

# Une modification locale n'est ecrasee que si le commit cible la contient deja : la fusion a
# 3 voies (base = HEAD, local, cible) doit donner exactement la cible, ou a defaut chaque bloc
# local doit exister dans la cible. Cas reel 2026-10-20 : correctif de bind ADVERTISE_HOST pose
# a la main (deja dans master) + bloc SMTP (remonte dans le repo).
local_changes_in_target() {
  local f tmp ok=0
  tmp=$(mktemp -d)
  for f in $(as_owner diff --name-only HEAD); do
    if [[ ! -f "$REPO/$f" ]] || ! as_owner show "$SHA:$f" > "$tmp/theirs" 2>/dev/null; then
      log "ERREUR : $f supprime localement ou absent du commit cible"; ok=1; continue
    fi
    as_owner show "HEAD:$f" > "$tmp/base" 2>/dev/null || : > "$tmp/base"
    cp "$REPO/$f" "$tmp/ours"
    if { ! git merge-file -q -p "$tmp/ours" "$tmp/base" "$tmp/theirs" > "$tmp/merged" \
         || ! cmp -s "$tmp/merged" "$tmp/theirs"; } \
       && ! blocks_in_target "$tmp/base" "$tmp/ours" "$tmp/theirs"; then
      log "ERREUR : la modification locale de $f n'est pas dans le commit cible"; ok=1
    fi
  done
  rm -rf "$tmp"
  return $ok
}

log "commit promu ${SHA:0:7} : mise a jour de $REPO"
as_owner fetch -q origin
if ! local_changes_in_target; then
  log "deploiement hote annule : modifications locales non presentes dans ${SHA:0:7} (rien n'est ecrase)"
  as_owner status --short --untracked-files=no >&2
  exit 1
fi
as_owner checkout -q -f --detach "$SHA"
bash "$REPO/infra/onsite/install.sh" --auto
echo "$SHA" > "$STATE_DIR/host-sha"
log "partie hote deployee au commit ${SHA:0:7}"
