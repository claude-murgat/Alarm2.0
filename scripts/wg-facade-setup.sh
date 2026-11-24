#!/bin/bash
# wg-facade-setup.sh — monte le tunnel WireGuard "facade" (wg1) vers le VPS OVH
# sur un noeud sur site : https://alarme.charlesmurgat.com (INV-199, infra/facade/).
#
# DISTINCT du mesh cluster : ce script ne touche JAMAIS wg0 (= mesh, le casserait).
# Il cree une interface SEPAREE wg1 (sous-reseau 10.200.0.0/24). Idempotent.
#
# Usage (en root), adresse = STD_FACADE_PLAN de infra/onsite/standard.env :
#   sudo bash /opt/alarm/scripts/wg-facade-setup.sh 10.200.0.7/24    # alarm-1 (.8 alarm-2, .9 alarm-3, .10 alarm-4)
#
# Apres : sur le VPS, ajouter la public key affichee comme [Peer] (AllowedIPs
# 10.200.0.X/32) et le BalancerMember (infra/facade/README.md). Le handshake
# monte alors via le PersistentKeepalive (25s).
#
# AUCUNE regle ufw sur wg1 : le VPS fait suivre le trafic entre TOUS ses clients
# WireGuard (10.200.0.2-.4 ne sont pas a nous) ; `ufw allow in on wg1` leur ouvrait
# SSH et tout le reste. Le backend 8000 reste joignable : Docker le publie sur
# 0.0.0.0 (ses regles iptables passent avant ufw). Regle laissee par une ancienne
# version : `sudo ufw delete allow in on wg1` (alarm-node-check : wg1_ufw KO).
set -euo pipefail

ADDR="${1:-}"
case "$ADDR" in 10.200.0.*/24) : ;; *) echo "usage: $0 <10.200.0.X/24>" >&2; exit 1 ;; esac
[ "$(id -u)" -eq 0 ] || { echo "A lancer en root (sudo)." >&2; exit 1; }

# Parametres facade OVH (cf peers.md ; meme cle que STD_FACADE_PUBKEY de standard.env)
OVH_PUBKEY="EO723NGhs9D1ZR7xPTVNUl+ZLAYYKxoQPNmXzuOWdz8="
OVH_ENDPOINT="51.77.222.190:51820"
FACADE_NET="10.200.0.0/24"

echo "[wg-facade] $(hostname) — wg1 facade (Address=$ADDR)"
echo "[wg-facade] wg0 (cluster) intact : $(ip -br addr show wg0 2>/dev/null || echo 'ABSENT?!')"

command -v wg >/dev/null || { apt-get update -qq && apt-get install -y -qq wireguard; }

umask 077
if [ ! -s /etc/wireguard/wg1-privatekey ]; then
  wg genkey | tee /etc/wireguard/wg1-privatekey | wg pubkey > /etc/wireguard/wg1-publickey
fi
chmod 600 /etc/wireguard/wg1-privatekey

cat > /etc/wireguard/wg1.conf <<EOF
[Interface]
Address = $ADDR
PrivateKey = $(cat /etc/wireguard/wg1-privatekey)
# MTU 1280 : WG sur 4G/cellulaire drop les gros paquets a 1420 (cf peers.md).
MTU = 1280

[Peer]
PublicKey = $OVH_PUBKEY
Endpoint = $OVH_ENDPOINT
AllowedIPs = $FACADE_NET
PersistentKeepalive = 25
EOF
chmod 600 /etc/wireguard/wg1.conf

systemctl enable wg-quick@wg1 >/dev/null 2>&1 || true
systemctl restart wg-quick@wg1
ip link set wg1 mtu 1280 2>/dev/null || true   # force le MTU si wg1 preexistait

echo "[wg-facade] PUBLIC KEY (a ajouter cote OVH) = $(cat /etc/wireguard/wg1-publickey)"
ip -br addr show wg1
wg show wg1
