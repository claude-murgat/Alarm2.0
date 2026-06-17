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
| `systemd/alarm-cd-pull.service` | `/etc/systemd/system/alarm-cd-pull.service` | Pull idempotent GHCR `:stable` (CD V1) |
| `systemd/alarm-cd-pull.timer` | `/etc/systemd/system/alarm-cd-pull.timer` | Timer 5 min pour le pull (CD V1) |
| `systemd/alarm-4g-standby.service` | `/etc/systemd/system/alarm-4g-standby.service` | Secours internet 4G en standby chaud (QMI, métrique 300) |
| `systemd/alarm-modem-rat.service` | `/etc/systemd/system/alarm-modem-rat.service` | Force `CNMP=54` (WCDMA+LTE sans 2G) au boot — coexistence voix+data |
| `systemd/alarm-modem-watchdog.service` | `/etc/systemd/system/alarm-modem-watchdog.service` | Watchdog modem : auto-récup d'un drop USB (PCI rescan) + alerte email |
| `install.sh` | *(exécutable)* | **Installe toute la stack modem** : units ci-dessus + scripts `/opt/alarm/{4g-standby.sh,modem-set-rat.py,modem-watchdog.py}` (sources dans `scripts/`) + code gateway `→ /opt/alarm-gateway/` + groupe `dialout` + drop-in `modem_gateway.py` |

## Stack modem on-site (gateway SMS/voix/contact + secours 4G + watchdog)

Le SIM7600 porte 4 fonctions : **contact sec → alarme**, **SMS + appels vocaux** d'escalade,
**secours internet 4G**, et un **watchdog** qui le récupère s'il drop du bus USB. Tout se
déploie d'un coup, de façon idempotente, depuis un checkout du repo sur le nœud :

```bash
sudo bash infra/onsite/install.sh
```

Le script copie les units + scripts, déploie le code gateway (`modem_gateway.py`, le superset
contact+voix+SMS — **pas** le legacy `sms_gateway.py`), ajoute `alarm-gateway` au groupe
`dialout` (sinon `Permission denied` sur `/dev/ttyUSB*`), pose le drop-in systemd, et enable
les services. Runbook complet : [`docs/FAILOVER_4G.md`](../../docs/FAILOVER_4G.md).
Prérequis secrets (non posés par le script) : `GATEWAY_KEY` dans `/etc/alarm-gateway.env`,
`SMTP_*` dans `/opt/alarm/.env.prod.{node<N>,secrets}` (alertes email du watchdog).

## Maintenance

**Si tu modifies une de ces configs sur une machine en prod, MAJ également ce dossier
dans le même commit.** Sinon la 2e machine on-site divergera silencieusement et le clone
deviendra impossible.

Pour cloner le standard sur une nouvelle machine, voir `docs/PROVISIONING_ONSITE.md` §21.
