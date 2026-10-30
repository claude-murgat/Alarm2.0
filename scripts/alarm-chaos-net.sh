#!/bin/bash
# alarm-chaos-net.sh — coupure reseau controlee de CE noeud (INV-177/178, mode « netcut »).
# Installe en root : /usr/local/bin/alarm-chaos-net (infra/onsite/install.sh, deploye partout
# par alarm-host-sync) ; appele depuis NODE3 par alarm-chaos.sh via `sudo -n` (sudoers alarm-cd).
#
#   alarm-chaos-net cut <S>   coupe wg0 dans 5 s, le remet S secondes (60..720) apres l'appel ;
#                             imprime ARMED (seule sortie standard) quand tout est arme
#   alarm-chaos-net restore   remet wg0 tout de suite (et annule une coupure pas encore executee),
#                             puis desarme le timer de restauration s'il attend encore
#
# Garantie INV-178 : la restauration est un timer systemd root DE CETTE MACHINE
# (alarm-chaos-restore, 10 tentatives a 30 s, journalisees), arme et verifie actif AVANT la
# coupure ; si l'armement echoue, rien n'est coupe. Il survit a la session SSH, a NODE3 et au
# VPN. La coupure (alarm-chaos-cut) est differee de 5 s : la session SSH du runner passe
# elle-meme par wg0 et doit se terminer proprement. Timers a AccuracySec=1s (defaut systemd :
# 1 min de marge, coupure et retour a +-60 s).
# Arguments invalides -> exit 2 sans rien faire ; wg-quick@wg0 inactif -> exit 1 sans rien faire.
set -euo pipefail

WG=wg-quick@wg0
usage() { echo "usage : alarm-chaos-net cut <secondes 60..720> | restore" >&2; exit 2; }
arm() {  # $1 secondes, $2 unit, puis options/commande ; stdout de systemd-run -> stderr (stdout = ARMED seul)
  local s=$1 unit=$2
  shift 2
  systemd-run --on-active="$s" --timer-property=AccuracySec=1s --unit="$unit" "$@" >&2
}
# Une tentative ($0 = unit, $1 = interface) : unit deja active -> rien (un `wg-quick down` couperait le tunnel remis) ;
# sinon `wg-quick down` d'abord : un wg0 residuel (ExecStop en erreur) ferait echouer start (« wg0 already exists »).
UP='systemctl is-active --quiet "$0" || { wg-quick down "$1" >/dev/null 2>&1 || true; systemctl start "$0"; }'
# Boucle de restauration : stdout du service transitoire -> journal (journalctl -u alarm-chaos-restore).
RETRY='i=1; until '"$UP"'; do echo "alarm-chaos-restore : $0, tentative $i/10 KO"; [ "$i" -ge 10 ] && { echo "alarm-chaos-restore : ECHEC, $0 non remis apres 10 tentatives"; exit 1; }; i=$((i + 1)); sleep 30; done; echo "alarm-chaos-restore : $0 remis (tentative $i)"'

case "${1:-}" in
  cut)
    S="${2:-}"
    # Decimal strict, 3 chiffres au plus : bash lirait 0777 en octal (511) et systemd en decimal (777 s) ;
    # un nombre geant deborderait l'arithmetique 64 bits de bash.
    [[ $# -eq 2 && "$S" =~ ^[1-9][0-9]{1,2}$ ]] && (( S >= 60 && S <= 720 )) || usage
    # wg0 monte a la main / par un autre gestionnaire, ou deja coupe : `systemctl stop` « reussirait » sans rien couper.
    systemctl is-active --quiet "$WG" || { echo "alarm-chaos-net : $WG inactif, rien n'est coupe" >&2; exit 1; }
    # Timers ou echec d'un test precedent encore charges : systemd-run --unit refuserait le doublon.
    systemctl stop alarm-chaos-cut.timer alarm-chaos-restore.timer 2>/dev/null || true
    systemctl reset-failed alarm-chaos-restore.service alarm-chaos-restore.timer alarm-chaos-cut.service 2>/dev/null || true
    # Sans --collect : un echec final de la restauration reste visible (systemctl --failed).
    arm "$S" alarm-chaos-restore /bin/sh -c "$RETRY" "$WG" wg0 \
      || { echo "alarm-chaos-net : armement de la restauration KO, rien n'est coupe" >&2; exit 1; }
    systemctl is-active --quiet alarm-chaos-restore.timer \
      || { echo "alarm-chaos-net : timer de restauration inactif, rien n'est coupe" >&2; exit 1; }
    arm 5 alarm-chaos-cut --collect systemctl stop "$WG"
    echo ARMED
    ;;
  restore)
    [[ $# -eq 1 ]] || usage
    # Coupure deja declenchee (timer echu) : son service aussi, sinon son `stop wg0` passerait apres la remise.
    systemctl stop alarm-chaos-cut.timer alarm-chaos-cut.service 2>/dev/null || true
    /bin/sh -c "$UP" "$WG" wg0
    sleep 2  # un stop wg0 deja en file s'execute ici : wg0 est reverifie avant de desarmer le filet
    /bin/sh -c "$UP" "$WG" wg0
    # wg0 remis : desarme le filet (sinon il tirerait a l'echeance, en plein test suivant) ; garde arme si wg0 KO (set -e).
    systemctl stop alarm-chaos-restore.timer 2>/dev/null || true
    ;;
  *) usage ;;
esac
