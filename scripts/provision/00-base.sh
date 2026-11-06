#!/usr/bin/env bash
# 00-base.sh — (root, sur la nouvelle machine) paquets de base, Docker CE du depot officiel, journaux
# Docker bornes. Prealable (README) : Debian 13 installe selon docs/STANDARD_MACHINE.md §4 et le depot
# clone dans /opt/alarm par l'utilisateur alarm. Idempotent.
#   sudo bash /opt/alarm/scripts/provision/00-base.sh
set -euo pipefail
. "$(dirname "$0")/lib.sh"
need_root; this_node
export DEBIAN_FRONTEND=noninteractive
say "$HOST ($NODE_NAME) : paquets de base"
apt-get update -qq
# efibootmgr / shim / grub signes : demarrage UEFI + Secure Boot du standard (deja la apres l'installeur).
apt-get install -y -qq git ca-certificates curl wireguard-tools efibootmgr shim-signed grub-efi-amd64-signed >/dev/null

say "Docker CE (download.docker.com)"
install -m0755 -d "$R/etc/apt/keyrings" "$R/etc/apt/sources.list.d" "$R/etc/docker"
[ -s "$R/etc/apt/keyrings/docker.asc" ] || curl -fsSL https://download.docker.com/linux/debian/gpg -o "$R/etc/apt/keyrings/docker.asc"
chmod a+r "$R/etc/apt/keyrings/docker.asc"
codename=$(. "$R/etc/os-release"; echo "$VERSION_CODENAME")
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $codename stable" \
  > "$R/etc/apt/sources.list.d/docker.list"
apt-get update -qq
apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null
J='{\n  "log-driver": "json-file",\n  "log-opts": {"max-size": "10m", "max-file": "3"}\n}\n'
if ! printf "$J" | cmp -s - "$R/etc/docker/daemon.json"; then
  printf "$J" > "$R/etc/docker/daemon.json"
  systemctl restart docker
  say "daemon.json : journaux json-file 10m x 3"
fi
usermod -aG docker alarm
say "FAIT. Suite : 10-harden.sh (scripts/provision/README.md)"
