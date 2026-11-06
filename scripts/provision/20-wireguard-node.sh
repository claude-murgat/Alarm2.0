#!/usr/bin/env bash
# 20-wireguard-node.sh [--force] — (root, sur la nouvelle machine) WireGuard de prod : cle de la machine
# (/etc/wireguard/prod-privatekey, creee une fois), wg0 a l'adresse du plan, NODE3 seul pair (relais
# vers 10.99.0.0/24) : rien a retoucher quand la machine change de LAN ou de site.
# Affiche la commande a lancer sur NODE3 (21-wireguard-node3.sh) ; relancer ensuite pour verifier la
# poignee de main. Un wg0.conf different (pair LAN historique) n'est remplace qu'avec --force (sauvegarde).
set -euo pipefail
. "$(dirname "$0")/lib.sh"
need_root; this_node
W="$R/etc/wireguard"
umask 077
install -d -m700 "$W"
[ -s "$W/prod-privatekey" ] || wg genkey > "$W/prod-privatekey"
wg pubkey < "$W/prod-privatekey" > "$W/prod-publickey"
pub=$(cat "$W/prod-publickey")
new=$(mktemp); trap 'rm -f "$new"' EXIT
cat > "$new" <<EOF
# Maillage prod Alarme Murgat — $HOST ($NODE_NAME, $WG_IP). Genere par scripts/provision/20-wireguard-node.sh.
# Tout passe par NODE3 (relais) : pas de pair LAN, rien a retoucher quand la machine demenage.
[Interface]
Address = $WG_IP/24
ListenPort = 51820
MTU = 1280
PrivateKey = $(cat "$W/prod-privatekey")

[Peer]
# NODE3 cloud OVH : relais vers tous les autres noeuds (10.99.0.0/24)
PublicKey = $STD_NODE3_PUBKEY
Endpoint = $STD_NODE3_ENDPOINT
AllowedIPs = 10.99.0.0/24
PersistentKeepalive = 25
EOF
if ! cmp -s "$new" "$W/wg0.conf"; then
  if [ -f "$W/wg0.conf" ]; then
    [ "${1:-}" = --force ] || die "$W/wg0.conf existe et differe du standard (pair LAN ?) : le relire, puis relancer avec --force"
    cp -a "$W/wg0.conf" "$W/wg0.conf.bak-$(date +%F-%H%M%S)"
  fi
  install -m600 "$new" "$W/wg0.conf"
  systemctl enable -q wg-quick@wg0
  systemctl restart wg-quick@wg0
  say "wg0.conf ecrit, wg0 relance ($WG_IP/24)"
fi
say "cle publique de $HOST : $pub"
say "sur NODE3 (root), si pas deja fait : bash /opt/alarm/scripts/provision/21-wireguard-node3.sh $WG_IP $pub $HOST"
i=${WG_WAIT_SECONDS:-15}
while :; do
  ts=$(wg show wg0 latest-handshakes 2>/dev/null | awk -v k="$STD_NODE3_PUBKEY" '$1 == k {print $2}') || true
  if [ "${ts:-0}" != 0 ]; then say "OK poignee de main avec NODE3"; exit 0; fi
  (( i-- > 0 )) || break
  sleep 1
done
say "pas encore de poignee de main avec NODE3 : declarer la cle sur NODE3 (ligne ci-dessus), puis relancer"
