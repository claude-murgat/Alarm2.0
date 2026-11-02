#!/bin/bash
# dns-fallback.sh [resolv.conf] [dhclient.conf] — INV-181 : un noeud sur site garde un DNS quand
# son reseau local tombe. Ajoute 1.1.1.1 puis 9.9.9.9 APRES les resolveurs existants (la box reste
# prioritaire, FAILOVER_4G §2.6) s'il n'y a aucun resolveur public. Appele par infra/onsite/install.sh.
# Idempotent ; un resolv.conf en lien (gere par un service) n'est pas touche.
set -euo pipefail
RESOLV="${1:-/etc/resolv.conf}"
DHCLIENT="${2:-/etc/dhcp/dhclient.conf}"
PUBLIC=(1.1.1.1 9.9.9.9)
say(){ echo "[dns-fallback] $*"; }
# Sans \n final, la ligne ajoutee se collerait a la derniere (et casserait le DNS au lieu de le secourir).
nl(){ [[ -z "$(tail -c1 "$1")" ]] || echo >> "$1"; }

# Renouvellement DHCP : sans cette ligne, dhclient reecrirait resolv.conf sans le secours.
if [[ -f "$DHCLIENT" ]] && ! grep -q "^append domain-name-servers" "$DHCLIENT"; then
  nl "$DHCLIENT"
  echo "append domain-name-servers 1.1.1.1, 9.9.9.9;  # INV-181, infra/onsite/install.sh" >> "$DHCLIENT"
  say "$DHCLIENT : secours DNS ajoute pour les renouvellements DHCP"
fi

if [[ -L "$RESOLV" ]]; then say "$RESOLV est un lien (gere par un service) : inchange"; exit 0; fi
if [[ ! -f "$RESOLV" ]]; then say "$RESOLV absent : inchange"; exit 0; fi
mapfile -t NS < <(awk '$1 == "nameserver" {print $2}' "$RESOLV")
for n in "${NS[@]}"; do
  for p in "${PUBLIC[@]}"; do [[ "$n" == "$p" ]] && exit 0; done
done
room=$(( 3 - ${#NS[@]} ))  # glibc ne lit que 3 resolveurs
if (( room <= 0 )); then say "$RESOLV : deja 3 resolveurs, aucun public : inchange (a corriger a la main)"; exit 0; fi
nl "$RESOLV"
for p in "${PUBLIC[@]:0:room}"; do echo "nameserver $p" >> "$RESOLV"; done
say "$RESOLV : secours public ajoute (${PUBLIC[*]:0:room}) apres ${NS[*]:-aucun}"
