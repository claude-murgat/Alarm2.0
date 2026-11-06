# scripts/provision/lib.sh — commun aux scripts de provisioning (a sourcer, pas a executer).
# Le plan des machines et les valeurs du standard viennent de infra/onsite/standard.env.
# Aucun mot de passe : sudo et ssh les demandent a l'operateur au moment voulu.
# Tests : ALARM_ROOT prefixe les chemins lus/ecrits, ALARM_STANDARD_ENV remplace standard.env.
R="${ALARM_ROOT:-}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=../../infra/onsite/standard.env
. "${ALARM_STANDARD_ENV:-$REPO/infra/onsite/standard.env}"
say(){ echo "[${0##*/}] $*"; }
die(){ echo "[${0##*/}] STOP : $*" >&2; exit 1; }
usage(){ echo "usage : ${0##*/} $*" >&2; exit 2; }
need_root(){ [ "$(id -u)" = 0 ] || die "a lancer en root (sudo bash $0)"; }
# plan <machine> : NODE_NAME, WG_IP, SITE de la machine d'apres STD_PLAN, sinon STOP.
plan(){
  local e
  for e in $STD_PLAN; do
    [ "${e%%=*}" = "$1" ] && { IFS=: read -r NODE_NAME WG_IP SITE <<<"${e#*=}"; return 0; }
  done
  die "machine « $1 » absente du plan (STD_PLAN de infra/onsite/standard.env) : mauvaise machine, ou hostname a corriger"
}
this_node(){ HOST=$(hostname -s); plan "$HOST"; }
