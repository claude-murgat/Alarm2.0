#!/usr/bin/env bash
# 50-gateway.sh — (root, sur la machine) passerelle au standard (INV-182) dans /etc/alarm-gateway.env :
# GATEWAY_KEY = celle de /opt/alarm/.env.prod.secrets (posee par 30-secrets.sh, jamais affichee),
# GATEWAY_SITE = site du plan, DRY_CONTACT_SOURCE=host (Arduino), DRY_CONTACT_NORMAL_VALUE=0 si absent.
# Le fichier garde ses droits (root:alarm-gateway 640, cree par infra/onsite/install.sh) et ses autres
# lignes. Passerelle relancee seulement si une valeur change. Idempotent.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
need_root; this_node
E="$R/etc/alarm-gateway.env"
[ -f "$E" ] || die "$E absent : lancer d'abord sudo bash $REPO/infra/onsite/install.sh"
key=$(sed -n 's/^GATEWAY_KEY=//p' "$REPO/.env.prod.secrets" 2>/dev/null | tail -1) || true
[ -n "$key" ] && [ "$key" != changeme-in-prod ] || die "GATEWAY_KEY absente de $REPO/.env.prod.secrets : lancer 30-secrets.sh depuis le poste"
umask 077
tmp=$(mktemp); trap 'rm -f "$tmp"' EXIT
changed=""
setkv(){  # <cle> <valeur> : remplace ou ajoute, meme inode (droits conserves), valeur jamais en argument d'une commande
  [ "$(sed -n "s/^$1=//p" "$E" | tail -1)" = "$2" ] && [ "$(grep -c "^$1=" "$E")" = 1 ] && return 0
  { grep -v "^$1=" "$E" || true; echo "$1=$2"; } > "$tmp"
  cat "$tmp" > "$E"
  changed="$changed $1"
}
setkv GATEWAY_KEY "$key"
setkv GATEWAY_SITE "$SITE"
setkv DRY_CONTACT_SOURCE host
grep -q '^DRY_CONTACT_NORMAL_VALUE=' "$E" || setkv DRY_CONTACT_NORMAL_VALUE 0
fp=$(printf %s "$key" | sha256sum | cut -c1-8)
if [ -n "$changed" ]; then
  systemctl restart alarm-sms-gateway
  say "$HOST : modifie :$changed ; passerelle relancee (site $SITE, empreinte de la cle $fp)"
else
  say "$HOST : deja au standard (site $SITE, empreinte de la cle $fp)"
fi
say "verifier : journalctl -u alarm-sms-gateway -n 20 (« Gateway operationnelle »), puis sudo alarm-node-check"
