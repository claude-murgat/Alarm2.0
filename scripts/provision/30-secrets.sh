#!/usr/bin/env bash
# 30-secrets.sh <alarm@IP-LAN> — (poste operateur) secrets de prod sur la nouvelle machine, en flux
# NODE3 -> machine : rien n'est ecrit sur le poste, aucun secret n'est affiche (empreintes seulement).
#   - /opt/alarm/.env.prod.secrets (600 : SECRET_KEY, GATEWAY_KEY, SMTP_*) et
#     /opt/alarm/backend/firebase-service-account.json, copies de NODE3 ; un flux vide n'ecrase rien ;
#   - cle SSH de l'orchestrateur CD (NODE3) dans ~alarm/.ssh/authorized_keys (une fois) ;
#   - empreintes comparees a NODE3, puis NODE3 -> alarm@10.99.0.X par cle (WireGuard deja monte).
# SSH : par cle (ssh-copy-id fait avant) ou mot de passe demande par ssh. NODE3 : NODE3_SSH (commande
# ssh complete), defaut ci-dessous. Idempotent.
set -uo pipefail
. "$(dirname "$0")/lib.sh"
T=${1:-}
[ -n "$T" ] || usage "<alarm@IP-LAN de la machine>   (env NODE3_SSH : commande ssh vers NODE3)"
N3=${NODE3_SSH:-ssh -p 50922 -i $HOME/.ssh/alarm_node3 -o ConnectTimeout=10 alarm@51.210.105.102}
A(){ ssh -o ConnectTimeout=10 "$T" "$@"; }
host=$(A hostname -s) || die "machine injoignable : $T"
plan "$host"
say "$T = $host ($NODE_NAME, $WG_IP)"

put(){  # <chemin sous /opt/alarm> <umask>
  $N3 "cat /opt/alarm/$1" \
    | A "umask $2; f=/opt/alarm/$1; cat > \$f.new && [ -s \$f.new ] && mv \$f.new \$f || { rm -f \$f.new; exit 1; }" \
    || die "copie de $1 depuis NODE3 en echec : rien n'est remplace"
  say "$1 : copie depuis NODE3"
}
put .env.prod.secrets 077
put backend/firebase-service-account.json 022

pub=$($N3 'cat ~/.ssh/id_ed25519.pub')
[[ "$pub" =~ ^ssh-ed25519\ [A-Za-z0-9+/=]+(\ [A-Za-z0-9@._-]+)?$ ]] || die "cle SSH de l'orchestrateur illisible sur NODE3"
A "install -d -m700 ~/.ssh && touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys \
   && { grep -qxF '$pub' ~/.ssh/authorized_keys || echo '$pub' >> ~/.ssh/authorized_keys; }" \
  || die "cle de l'orchestrateur non posee"
say "cle SSH de l'orchestrateur CD : presente"

# Empreintes (8 premiers caracteres du SHA-256) : jamais la valeur.
fp='cd /opt/alarm && for k in SECRET_KEY GATEWAY_KEY SMTP_USER; do printf "%s=%s " $k $(sed -n "s/^$k=//p" .env.prod.secrets | tail -1 | sha256sum | cut -c1-8); done; echo firebase=$(sha256sum < backend/firebase-service-account.json | cut -c1-8)'
ref=$($N3 "$fp"); got=$(A "$fp")
say "empreintes NODE3   : $ref"
say "empreintes $host : $got"
[ -n "$ref" ] && [ "$ref" = "$got" ] || die "empreintes differentes de NODE3"
[ "$(A 'stat -c %a /opt/alarm/.env.prod.secrets')" = 600 ] || die ".env.prod.secrets pas en 600"

$N3 "ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new alarm@$WG_IP true" \
  || die "NODE3 ne joint pas alarm@$WG_IP par cle (wg0 monte ? 20 et 21 faits ? ufw 22 depuis 10.99.0.0/24 ?)"
say "OK : NODE3 joint alarm@$WG_IP par cle (deploiement CD). Suite : 40-join.sh sur la machine"
