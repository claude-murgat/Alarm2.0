#!/usr/bin/env bash
# 60-change-ip.sh <nouvelle-IP/prefixe> <passerelle> — (root, sur la machine) change l'adresse fixe du
# LAN (ifupdown, stanza « iface eno1 inet static » de /etc/network/interfaces, sauvegarde .bak), puis
# redemarre dans 5 s pour l'appliquer. Refuse une adresse qui repond deja (conflit, echange de cables).
#   sudo bash /opt/alarm/scripts/provision/60-change-ip.sh 172.16.1.118/16 172.16.0.1
# Apres : ssh-keygen -R <ancienne IP> sur le poste ; repointer les pairs WireGuard LAN qui visaient
# l'ancienne IP (piege connu, docs/STANDARD_MACHINE.md §9) ; LAN change -> relancer 10-harden.sh.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
need_root; this_node
new=${1:-}; gw=${2:-}
python3 - "$new" "$gw" <<'PY' || usage "<nouvelle-IP/prefixe> <passerelle du meme reseau>   (ex. 192.168.1.52/24 192.168.1.1)"
import ipaddress, sys
new, gw = sys.argv[1], sys.argv[2]
i = ipaddress.IPv4Interface(new)  # adresse invalide -> exception -> code 1
g = ipaddress.IPv4Address(gw)
sys.exit(0 if "/" in new and g in i.network and g != i.ip else 1)
PY
F="$R/etc/network/interfaces"
grep -q "^iface $STD_LAN_IFACE inet static" "$F" || die "pas de « iface $STD_LAN_IFACE inet static » dans $F"
ping -c1 -W2 "${new%/*}" >/dev/null 2>&1 && die "${new%/*} repond deja : conflit d'adresse (machine branchee sur le mauvais cable ?)"
cp -a "$F" "$F.bak-$(date +%F-%H%M%S)"
# Seulement dans la stanza du LAN : de sa ligne iface jusqu'a la prochaine stanza.
sed -i -E "/^iface $STD_LAN_IFACE inet static/,/^(iface|auto|allow-|mapping|source)/{
  s#^([[:space:]]+address[[:space:]]+).*#\1$new#
  s#^([[:space:]]+gateway[[:space:]]+).*#\1$gw#
}" "$F"
grep -qE "^[[:space:]]+address[[:space:]]+$new$" "$F" || die "adresse non modifiee dans $F (format inattendu, a corriger a la main)"
sed -n "/^iface $STD_LAN_IFACE inet static/,/^$/p" "$F"
say "$HOST : $new via $gw au prochain demarrage ; redemarrage dans 5 s"
say "ensuite : ssh alarm@${new%/*} ; pairs WireGuard LAN pointant l'ancienne IP a repointer ; nouveau LAN -> 10-harden.sh"
systemd-run --on-active=5 systemctl reboot
