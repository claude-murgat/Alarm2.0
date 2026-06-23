#!/bin/bash
# wg-facade-setup.sh — monte le tunnel WireGuard "facade" (wg1) vers le VPS OVH
# sur un noeud onsite, pour exposer l'UI alarme publiquement (reverse-proxy OVH).
#
# DISTINCT du mesh cluster : ce script ne touche JAMAIS wg0 (= mesh, le casserait).
# Il cree une interface SEPAREE wg1 (sous-reseau 10.200.0.0/24). Idempotent.
#
# Usage (en root) :
#   sudo bash scripts/wg-facade-setup.sh 10.200.0.5/24   # alarme1 (onsite-1)
#   sudo bash scripts/wg-facade-setup.sh 10.200.0.6/24   # alarme2 (onsite-2)
#
# Apres : ajouter la public key affichee comme [Peer] cote OVH (AllowedIPs
# 10.200.0.X/32). Le handshake monte alors via le PersistentKeepalive (25s).
# Cf. infra/onsite/wg1-facade.conf.example + infra/onsite/peers.md.
set -euo pipefail

ADDR="${1:-}"
case "$ADDR" in 10.200.0.*/24) : ;; *) echo "usage: $0 <10.200.0.X/24>" >&2; exit 1 ;; esac
[ "$(id -u)" -eq 0 ] || { echo "A lancer en root (sudo)." >&2; exit 1; }

# Parametres facade OVH (cf peers.md)
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

if ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw allow in on wg1 >/dev/null 2>&1 && echo "[wg-facade] ufw: allow in on wg1"
fi

echo "[wg-facade] PUBLIC KEY (a ajouter cote OVH) = $(cat /etc/wireguard/wg1-publickey)"
ip -br addr show wg1
wg show wg1
