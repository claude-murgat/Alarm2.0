#!/bin/bash
# infra/node3/install.sh — installe / met a jour la partie hote de NODE3 (cloud) : orchestrateur CD, canary,
# rollback, tirage d'images, sauvegarde de la base, envoi d'email, leurs units et alarm-host-sync (INV-141,
# etendu a NODE3 le 2026-11-13). Jamais la pile modem, la passerelle ni la sudoers du site (infra/onsite/install.sh).
#
#   Bootstrap, une fois : sudo bash /opt/alarm/infra/node3/install.sh
#   Ensuite alarm-host-sync le relance seul en --auto a chaque commit promu :stable (drop-in node3.conf).
#
# --auto : n'active ni ne demarre aucun timer (un timer arrete pour une maintenance le reste) ; relance
# seulement un timer actif dont l'unit a change. Un fichier inchange n'est pas reecrit.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
ROOT="${ROOT:-}"  # racine temporaire des tests
AUTO=0; [ "${1:-}" = "--auto" ] && AUTO=1
say() { echo "[node3-install] $*"; }
CHANGED=" "
inst() {  # <source dans le depot> <destination> <mode>
  cmp -s "$REPO/$1" "$ROOT$2" && return 0
  install -D -m"$3" "$REPO/$1" "$ROOT$2"
  say "$2"; CHANGED="$CHANGED$2 "
}

inst scripts/alarm-cd-orchestrate.sh /usr/local/bin/alarm-cd-orchestrate 755
inst scripts/alarm-cd-canary.sh      /usr/local/bin/alarm-cd-canary.sh   755
inst scripts/alarm-cd-rollback.sh    /usr/local/bin/alarm-cd-rollback.sh 755
inst scripts/alarm-cd-pull.sh        /usr/local/bin/alarm-cd-pull        755
inst scripts/alarm-db-backup.sh      /usr/local/bin/alarm-db-backup      755
inst scripts/alarm-host-sync.sh      /usr/local/bin/alarm-host-sync      755
inst scripts/send-alert-email.py     /opt/alarm/send-alert-email.py      755
TIMERS="alarm-cd-orchestrate alarm-cd-pull alarm-db-backup alarm-db-restore-test alarm-host-sync"
for u in $TIMERS; do
  inst "infra/onsite/systemd/$u.service" "/etc/systemd/system/$u.service" 644
  inst "infra/onsite/systemd/$u.timer"   "/etc/systemd/system/$u.timer"   644
done
inst infra/node3/alarm-host-sync.service.d/node3.conf /etc/systemd/system/alarm-host-sync.service.d/node3.conf 644

if [ "$AUTO" = 0 ]; then
  systemctl daemon-reload
  for u in $TIMERS; do systemctl enable --now "$u.timer"; done
  say "bootstrap fait : timers actifs ($TIMERS)"
  exit 0
fi
[[ "$CHANGED" == *" /etc/systemd/"* ]] && systemctl daemon-reload
for u in $TIMERS; do
  if [[ "$CHANGED" == *" /etc/systemd/system/$u.timer "* ]] && systemctl is-active --quiet "$u.timer"; then
    systemctl restart "$u.timer"; say "relance $u.timer"
  fi
done
say "partie hote NODE3 a jour"
