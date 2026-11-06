#!/usr/bin/env bash
# 21-wireguard-node3.sh <10.99.0.X> <cle publique> [nom] — (root, sur NODE3) declare le pair WireGuard
# d'une machine sur site, relaye par NODE3 : AllowedIPs 10.99.0.X/32, pas d'Endpoint (machine derriere
# NAT, apprise par son PersistentKeepalive). Ajout au fichier (sauvegarde) + `wg set` a chaud.
# Idempotent (cle deja declaree : rien n'est ajoute). Refuse une IP deja portee par une AUTRE cle :
# remplacement de machine -> retirer d'abord l'ancien [Peer] (docs/REPLACE_ONSITE_NODE.md §2).
set -euo pipefail
. "$(dirname "$0")/lib.sh"
need_root
ip=${1:-}; key=${2:-}; name=${3:-machine}
[[ "$ip" =~ ^10\.99\.0\.([0-9]{1,3})$ ]] && (( BASH_REMATCH[1] >= 2 && BASH_REMATCH[1] <= 254 )) \
  || usage "<10.99.0.X (2..254, pas NODE3)> <cle publique> [nom]"
[[ "$key" =~ ^[A-Za-z0-9+/]{43}=$ ]] || usage "<10.99.0.X> <cle publique WireGuard (44 caracteres base64)> [nom]"
C="$R/etc/wireguard/wg0.conf"
grep -qE '^Address *= *10\.99\.0\.1/' "$C" 2>/dev/null || die "pas NODE3 ($C sans Address 10.99.0.1) : a lancer sur NODE3"
if grep -qF -- "$key" "$C"; then
  say "pair $key deja declare"
else
  grep -qE "^AllowedIPs *= *${ip//./\\.}/32 *$" "$C" \
    && die "$ip/32 deja declaree pour une autre cle : retirer l'ancien [Peer] de $C (remplacement de machine), puis relancer"
  cp -a "$C" "$C.bak-$(date +%F-%H%M%S)"
  printf '\n[Peer]\n# %s (%s) : derriere NAT, adresse apprise par keepalive, relaye par NODE3\nPublicKey = %s\nAllowedIPs = %s/32\n' \
    "$name" "$ip" "$key" "$ip" >> "$C"
  say "pair $name ($ip) ajoute a $C"
fi
wg set wg0 peer "$key" allowed-ips "$ip/32"
[ "$(cat "$R/proc/sys/net/ipv4/ip_forward" 2>/dev/null)" = 1 ] \
  || say "ATTENTION : net.ipv4.ip_forward != 1 : NODE3 ne relaie pas (infra/onsite/peers.md)"
say "a mettre a jour : table des cles publiques de infra/onsite/peers.md"
