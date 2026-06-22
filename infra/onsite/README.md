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
| `wg0.conf.example` | `/etc/wireguard/wg0.conf` (avec privatekey injectée) | Mesh Wireguard cluster (10.99.0.x) |
| `wg1-facade.conf.example` | `/etc/wireguard/wg1.conf` (via `scripts/wg-facade-setup.sh`) | **2ᵉ** tunnel WG vers le VPS OVH façade (10.200.0.x) — expose l'UI alarme publiquement. Interface séparée, ne touche pas wg0 |
| `peers.md` | (doc seulement, pas de cible) | Index des public keys + plans d'adressage WG (mesh **et** façade OVH) |
| `systemd/alarm-cd-pull.service` | `/etc/systemd/system/alarm-cd-pull.service` | Pull idempotent GHCR `:stable` (CD V1) |
| `systemd/alarm-cd-pull.timer` | `/etc/systemd/system/alarm-cd-pull.timer` | Timer 5 min pour le pull (CD V1) |
| `systemd/alarm-4g-standby.service` | `/etc/systemd/system/alarm-4g-standby.service` | Secours internet 4G en standby chaud (QMI, métrique 300) |
| `systemd/alarm-modem-rat.service` | `/etc/systemd/system/alarm-modem-rat.service` | Force `CNMP=54` (WCDMA+LTE sans 2G) au boot — coexistence voix+data |
| `systemd/alarm-modem-watchdog.service` | `/etc/systemd/system/alarm-modem-watchdog.service` | Watchdog modem : auto-récup d'un drop USB (PCI rescan) + alerte email |
| `systemd/alarm-disk-check.service` | `/etc/systemd/system/alarm-disk-check.service` | Sonde remplissage disque (`/` + `/var/lib/docker`) → alerte email si seuil dépassé (oneshot, lancé par le timer) |
| `systemd/alarm-disk-check.timer` | `/etc/systemd/system/alarm-disk-check.timer` | Timer 15 min de la sonde disque (garde-fou anti-saturation, cf incident WAL 2026-06-22) |
| `install.sh` | *(exécutable)* | **Installe toute la stack modem + la supervision disque** : units ci-dessus + scripts `/opt/alarm/{4g-standby.sh,modem-set-rat.py,modem-watchdog.py,disk-check.sh,send-alert-email.py}` (sources dans `scripts/`) + code gateway `→ /opt/alarm-gateway/` + groupe `dialout` + drop-in `modem_gateway.py` |

## Stack modem on-site (gateway SMS/voix + secours 4G + watchdog)

Le SIM7600 porte 3 fonctions : **SMS + appels vocaux** d'escalade, **secours internet 4G**,
et un **watchdog** qui le récupère s'il drop du bus USB. (Le **contact sec** est lu depuis
2026-06-18 par un **Arduino UNO R4 sur USB**, `DRY_CONTACT_SOURCE=host` — **découplé du modem**
pour qu'il survive à un drop USB ; cf INV-120 + `gateway/firmware/dry_contact_r4/`.) Tout se
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

## Supervision disque (garde-fou anti-saturation)

Le **2026-06-22** le cluster est tombé en entier (plus de leader Patroni, alarme
incapable de sonner) parce que du **WAL a saturé les disques** de node1 (207 Go de WAL
→ 100 %) et de node3 (cloud, 100 %) ; PostgreSQL ne redémarrait plus. Surtout, la panne
est restée **silencieuse 2,5 jours** : *rien* ne surveillait le remplissage disque.

`alarm-disk-check.timer` (15 min) lance [`scripts/disk-check.sh`](../../scripts/disk-check.sh),
qui vérifie `/` et `/var/lib/docker` via `df` et envoie **un** email par épisode au-delà
d'un seuil (**WARN ~85 %**, **CRIT ~92 %**), re-notifie un problème persistant au plus
toutes les 6 h, puis un email de retour à la normale. L'envoi réutilise **exactement** la
même voie SMTP que le watchdog modem — [`scripts/send-alert-email.py`](../../scripts/send-alert-email.py),
mêmes secrets `/opt/alarm/.env.prod.*`. Aucune dépendance (bash + `df` + `python3` stdlib).

Posé sur **les 3 nœuds** par `install.sh` (le timer est `enable --now` : une sonde
read-only sans risque, et ne pas la démarrer reproduirait l'incident). **node3** (cloud,
sans modem) n'a besoin que de la sonde + des `SMTP_*` dans `/opt/alarm/.env.prod.{node3,secrets}`
— les avertissements modem de `install.sh` y sont attendus et sans effet.

```bash
sudo /opt/alarm/disk-check.sh --dry-run      # affiche les % et niveaux, n'envoie rien
sudo /opt/alarm/disk-check.sh --test-email   # vérifie la chaîne SMTP de bout en bout
```

Seuils et chemins surchargeables par variables d'env (`DISK_WARN_PCENT`, `DISK_CRIT_PCENT`,
`DISK_CHECK_PATHS`, `DISK_RENOTIFY_HOURS`) — cf en-tête du script.

## Exposition publique via VPS OVH (tunnel façade `wg1`)

En plus du mesh cluster (`wg0`, 10.99.0.x), chaque onsite monte un **2ᵉ tunnel
WireGuard `wg1`** (sous-réseau `10.200.0.0/24`) vers un **VPS OVH à IP fixe** qui
reverse-proxy l'UI alarme (port `8000`) vers l'extérieur. Interface **séparée** —
`wg0` n'est jamais touché. Roaming complet (survit au déménagement de site).

```bash
sudo bash scripts/wg-facade-setup.sh 10.200.0.5/24   # alarme1 (onsite-1)
sudo bash scripts/wg-facade-setup.sh 10.200.0.6/24   # alarme2 (onsite-2)
```

Détails (adressage, clés publiques à déclarer côté OVH, MTU 1280, ufw) :
[`peers.md` § Tunnel façade OVH](peers.md). Template : [`wg1-facade.conf.example`](wg1-facade.conf.example).

## Maintenance

**Si tu modifies une de ces configs sur une machine en prod, MAJ également ce dossier
dans le même commit.** Sinon la 2e machine on-site divergera silencieusement et le clone
deviendra impossible.

Pour cloner le standard sur une nouvelle machine, voir `docs/PROVISIONING_ONSITE.md` §21.
