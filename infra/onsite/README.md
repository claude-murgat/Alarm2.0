# `infra/onsite/` — Configs versionnées des nœuds on-site

Ce dossier contient les fichiers de configuration de référence pour le provisioning des
nœuds on-site du cluster Alarme Murgat. Voir **[`docs/PROVISIONING_ONSITE.md`](../../docs/PROVISIONING_ONSITE.md)**
pour la procédure complète et les explications.

## Contenu

| Fichier | Destination sur la machine | Sujet |
|---|---|---|
| `ssh/99-hardening.conf` | `/etc/ssh/sshd_config.d/99-hardening.conf` | Désactive password auth + root SSH |
| `fail2ban/sshd.local` | `/etc/fail2ban/jail.d/sshd.local` | Jail SSH (permissif maxretry=100 phase install) |
| `apt.conf.d/52unattended-upgrades-local` | `/etc/apt/apt.conf.d/52unattended-upgrades-local` | Security-only, no auto-reboot |
| `apt.conf.d/20auto-upgrades` | `/etc/apt/apt.conf.d/20auto-upgrades` | Active les timers MAJ |
| `chrony.conf` | `/etc/chrony/chrony.conf` | NTP — pools FR + Debian |
| `journald.conf.d/persistent.conf` | `/etc/systemd/journald.conf.d/persistent.conf` | Logs persistants 2 Go max |
| `sysctl.d/99-alarm-postgres.conf` | `/etc/sysctl.d/99-alarm-postgres.conf` | swappiness=1, dirty ratios bas |
| `wg0.conf.example` | `/etc/wireguard/wg0.conf` (avec privatekey injectée) | Mesh Wireguard |
| `peers.md` | (doc seulement, pas de cible) | Index des public keys et plan d'adressage Wireguard |

## Maintenance

**Si tu modifies une de ces configs sur une machine en prod, MAJ également ce dossier
dans le même commit.** Sinon la 2e machine on-site divergera silencieusement et le clone
deviendra impossible.

Pour cloner le standard sur une nouvelle machine, voir `docs/PROVISIONING_ONSITE.md` §21.
