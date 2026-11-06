#!/usr/bin/env bash
# 10-harden.sh [--ssh] — (root, sur la nouvelle machine) durcissement et services de base du standard
# (docs/PROVISIONING_ONSITE.md §2-§12) : ModemManager masque, ufw, fail2ban, mises a jour de securite,
# chrony, journal persistant, sysctl, watchdog systemd, SMART, pull des images (alarm-cd-pull).
# Pare-feu : SSH et backend ouverts au sous-reseau ACTUEL du LAN (eno1) ; apres un changement de LAN
# (demenagement), relancer ce script. --ssh : coupe le mot de passe SSH (une cle doit etre deployee).
# Idempotent. Ensuite : sudo bash infra/onsite/install.sh (passerelle, pile modem, timers).
set -euo pipefail
. "$(dirname "$0")/lib.sh"
need_root; this_node
I="$REPO/infra/onsite"
lan=$(ip -4 -o addr show dev "$STD_LAN_IFACE" | awk '{print $4; exit}') || true
[ -n "$lan" ] || die "pas d'adresse IPv4 sur $STD_LAN_IFACE"
net=$(python3 -c 'import ipaddress, sys; print(ipaddress.ip_interface(sys.argv[1]).network)' "$lan")
if [ "${1:-}" = --ssh ] && [ ! -s "$R/home/alarm/.ssh/authorized_keys" ]; then
  die "aucune cle dans ~alarm/.ssh/authorized_keys : mot de passe SSH garde (ssh-copy-id depuis le poste d'abord)"
fi

say "$HOST : ModemManager masque (il prendrait le port du SIM7600)"
systemctl mask --now ModemManager >/dev/null 2>&1 || true
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ufw fail2ban unattended-upgrades chrony smartmontools >/dev/null

say "pare-feu : LAN $net, maillage WireGuard"
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow from "$net" to any port 22 proto tcp comment 'SSH LAN site' >/dev/null
ufw allow from 10.99.0.0/24 to any port 22 proto tcp comment 'SSH mesh WG prod (CD orchestrator)' >/dev/null
ufw allow 51820/udp comment 'Wireguard' >/dev/null
ufw allow in on wg0 comment 'Wireguard mesh prod inter-noeuds' >/dev/null
ufw allow from "$net" to any port 8000 proto tcp comment 'Backend LAN site (apps Android)' >/dev/null
ufw --force enable >/dev/null

say "fail2ban, mises a jour de securite, chrony"
install -D -m644 "$I/fail2ban/sshd.local" "$R/etc/fail2ban/jail.d/sshd.local"
systemctl enable -q --now fail2ban; systemctl restart fail2ban
install -D -m644 "$I/apt.conf.d/20auto-upgrades" "$R/etc/apt/apt.conf.d/20auto-upgrades"
install -D -m644 "$I/apt.conf.d/52unattended-upgrades-local" "$R/etc/apt/apt.conf.d/52unattended-upgrades-local"
install -D -m644 "$I/chrony.conf" "$R/etc/chrony/chrony.conf"
systemctl disable --now systemd-timesyncd >/dev/null 2>&1 || true
systemctl enable -q --now chrony; systemctl restart chrony

say "journal persistant, sysctl, watchdog systemd (30 s), SMART"
install -D -m644 "$I/journald.conf.d/persistent.conf" "$R/etc/systemd/journald.conf.d/persistent.conf"
install -d "$R/var/log/journal"
systemd-tmpfiles --create --prefix /var/log/journal; systemctl restart systemd-journald
install -D -m644 "$I/sysctl.d/99-alarm-postgres.conf" "$R/etc/sysctl.d/99-alarm-postgres.conf"
sysctl -q --system
install -d "$R/etc/systemd/system.conf.d"
printf '[Manager]\nRuntimeWatchdogSec=%s\nRebootWatchdogSec=10min\n' "$STD_RUNTIME_WATCHDOG" > "$R/etc/systemd/system.conf.d/watchdog.conf"
systemctl daemon-reexec
systemctl enable -q --now smartd 2>/dev/null || true

say "CD : pull des images :stable (alarm-cd-pull, absent de install.sh)"
install -D -m755 "$REPO/scripts/alarm-cd-pull.sh" "$R/usr/local/bin/alarm-cd-pull"
install -D -m644 "$I/systemd/alarm-cd-pull.service" "$R/etc/systemd/system/alarm-cd-pull.service"
install -D -m644 "$I/systemd/alarm-cd-pull.timer" "$R/etc/systemd/system/alarm-cd-pull.timer"
systemctl daemon-reload; systemctl enable -q --now alarm-cd-pull.timer

if [ "${1:-}" = --ssh ]; then
  install -D -m644 "$I/ssh/99-hardening.conf" "$R/etc/ssh/sshd_config.d/99-hardening.conf"
  sshd -t; systemctl reload ssh
  say "SSH : mot de passe coupe (cle seulement)"
else
  say "SSH : mot de passe garde (--ssh pour le couper, une fois la cle deployee)"
fi
say "FAIT. Suite : sudo bash $REPO/infra/onsite/install.sh, puis 20-wireguard-node.sh"
