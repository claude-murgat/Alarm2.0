#!/bin/bash
# alarm-chaos-net.sh — coupure reseau controlee de CE noeud (INV-177/178, mode « netcut »).
# Installe en root : /usr/local/bin/alarm-chaos-net (infra/onsite/install.sh, deploye partout
# par alarm-host-sync) ; appele depuis NODE3 par alarm-chaos.sh via `sudo -n` (sudoers alarm-cd).
#
#   alarm-chaos-net cut <S>   coupe wg0 dans 5 s, le remet S secondes (60..720) apres l'appel
#   alarm-chaos-net restore   remet wg0 tout de suite (et annule une coupure pas encore executee)
#
# Garantie INV-178 : la restauration est un timer systemd root DE CETTE MACHINE
# (alarm-chaos-restore), arme AVANT la coupure ; si l'armement echoue, rien n'est coupe.
# Il survit a la session SSH, a NODE3 et au VPN. La coupure (alarm-chaos-cut) est differee de
# 5 s : la session SSH du runner passe elle-meme par wg0 et doit se terminer proprement.
# Arguments invalides -> exit 2 sans rien faire.
set -euo pipefail

WG=wg-quick@wg0
usage() { echo "usage : alarm-chaos-net cut <secondes 60..720> | restore" >&2; exit 2; }

case "${1:-}" in
  cut)
    S="${2:-}"
    [[ $# -eq 2 && "$S" =~ ^[0-9]+$ ]] && (( S >= 60 && S <= 720 )) || usage
    # Timers d'un test precedent encore charges : systemd-run --unit refuserait le doublon.
    systemctl stop alarm-chaos-cut.timer alarm-chaos-restore.timer 2>/dev/null || true
    systemd-run --on-active="$S" --unit=alarm-chaos-restore --collect systemctl start "$WG" \
      || { echo "alarm-chaos-net : armement de la restauration KO, rien n'est coupe" >&2; exit 1; }
    systemd-run --on-active=5 --unit=alarm-chaos-cut --collect systemctl stop "$WG"
    ;;
  restore)
    [[ $# -eq 1 ]] || usage
    systemctl stop alarm-chaos-cut.timer 2>/dev/null || true
    systemctl start "$WG"
    ;;
  *) usage ;;
esac
