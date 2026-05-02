# Provisioning d'un nœud on-site — Alarme Murgat

> **Standard de référence pour le provisioning des machines on-site du cluster Patroni.**
> Si tu modifies une machine en prod, **mets à jour ce fichier dans le même commit** sinon
> les machines divergeront silencieusement et le clone deviendra impossible.
> Cette doc est vivante : section "Améliorations à venir" et `<!-- gotcha: -->` à
> enrichir au fil des apprentissages.

---

## 0. Vue d'ensemble

Le cluster Patroni de production cible la topologie **2 on-site + 1 hors-site cloud** (révision
2026-04-25 dans `SITE_SECURITY_NOTES.md`). Chaque nœud on-site héberge :

- Backend FastAPI
- PostgreSQL géré par Patroni (réplication streaming entre nœuds)
- etcd pour le consensus distribué Patroni
- Gateway SMS/voix Waveshare SIM7600 (Phase 2 — pas dans cette doc V1)

Cette doc décrit le provisioning **Phase 1** : OS hardening, réseau (Wireguard), Docker,
bases pour cluster et CD. La SIM7600 et la gateway sont en Phase 2 (séparée).

### Conventions

- User d'admin/ops sur la machine : `alarm`
- IP Wireguard internes : `10.99.0.1/24` (cloud), `10.99.0.2/24` (onsite-1), `10.99.0.3/24` (onsite-2)
- Hostname : `onsite-1.alarm.local`, `onsite-2.alarm.local`
- Code & configs versionnées : repo `claude-murgat/Alarm2.0` cloné dans `/opt/alarm`
- Configs système versionnées : `infra/onsite/` du repo, déployées par symlink ou copie

### Première machine vs clone

La première machine on-site provisionnée par cette doc est `onsite-2` (172.16.1.120, mai 2026).
La 2e (`onsite-1`) suit le même chemin en remplaçant les valeurs marquées `<!-- onsite-2 -->`
(IP Wireguard, hostname, port host backend si conflit avec un autre nœud sur la même baie).

---

## 1. Pré-requis matériels et système

### Minimums requis (à compléter avec specs réelles)

| Élément | Cible | Pourquoi |
|---|---|---|
| RAM | ≥ 8 Go | Patroni + Postgres + Backend + etcd + buffers |
| Disque | ≥ 120 Go SSD | OS + Docker images + pgdata + WAL + logs + 20% snapshots LVM |
| CPU | 2 cœurs minimum | Goertzel DTMF + Patroni heartbeats |
| Watchdog hardware | iTCO/AMD ou TPM | Reboot auto si kernel locked |
| USB libre | 1 port USB micro-B | Waveshare SIM7600 (Phase 2) |
| OS | Debian 12 (bookworm) | Aligné CI |

### État initial constaté (machine onsite-2, 172.16.1.120, 2026-05-01)

| Élément | Valeur | Conséquence |
|---|---|---|
| Hardware | Shuttle NC03U (mini-PC, BIOS 2016-12-13, 9 ans) | OK, à monitorer (vieux firmware) |
| OS | Debian 13 (trixie) | Plus récent que Debian 12 cible — adapter dépôts Docker |
| Kernel | 6.12.74+deb13+1-amd64 | OK |
| CPU | Intel Celeron 3865U @ 1.8 GHz, 2 cœurs / 2 threads | Modeste mais suffisant pour le workload Docker |
| RAM | **3.7 Go** (4 Go installé) | **Sous le minimum cible 8 Go.** Tendu pour Patroni + Postgres + Backend + etcd. Garder le swap, `vm.swappiness=1`, Postgres `shared_buffers=256MB`. Améliorer si possible (+RAM) |
| Disque | `/dev/sda1` ext4 28 Go (25 Go libres), swap 1.6 Go | **Non-LVM** — pas de snapshot possible, pas de séparation Docker/PG. À reconsidérer au prochain reset |
| Watchdog | iTCO_wdt actif, `/dev/watchdog0`, timeout 30s | ✅ Hardware watchdog disponible |
| Time sync | systemd-timesyncd actif mais non synchronisé | À remplacer par chrony |
| ModemManager | inactive + not-found | ✅ Pas d'interférence avec SIM7600 |
| SIM7600 | Bus 001 Device 015 SimTech ; `/dev/ttyUSB0..4` libres | Branchée et visible. Drivers `option`/`cdc_*` natifs Linux fonctionnent |
| Réseau | `enp1s0` 172.16.1.120/16, GW 172.16.0.1 ; `wlp2s0` down ; `wwp0s20f0u4i5` down (interface 4G) | Carte gigabit Intel I211 + WiFi RTL8188EE (non utilisé) |
| Hostname actuel | `alarm-1` | À changer en `onsite-2.alarm.local` |
| User `alarm` | uid=1000, groupes : `alarm,sudo,audio,dip,video,plugdev,users,netdev,bluetooth` | OK, sudo dispo |
| sudoers | Seulement le README par défaut | Vierge, base saine |
| Packages clés | ssh 10.0p1, systemd-timesyncd | À installer : ufw, fail2ban, chrony, wireguard, docker, smartmontools, unattended-upgrades, pgbackrest |

---

## 2. Premier accès et capture de l'état initial

### 2.1. Connexion initiale

User `alarm` existant avec mot de passe (transmis hors-doc — **ne jamais inscrire dans cette doc
ni dans le repo**). Aucune clé SSH déployée à ce stade → première connexion par mot de passe
obligatoire.

```bash
# Sur la machine de dev :
ssh-keygen -t ed25519 -N "" -C "alarm-onsite-X-provisioning" -f ~/.ssh/alarm_onsite_X

# Premier déploiement de la clé via sshpass (one-shot) :
SSHPASS='<password>' sshpass -e ssh-copy-id \
  -o StrictHostKeyChecking=accept-new \
  -i ~/.ssh/alarm_onsite_X.pub \
  alarm@<IP>

# Validation :
ssh -i ~/.ssh/alarm_onsite_X alarm@<IP> 'echo OK'
```

### 2.2. Capture de l'état initial

Avant toute modification :

```bash
ssh -i ~/.ssh/alarm_onsite_X alarm@<IP> '
  echo "=== OS ==="; cat /etc/os-release; uname -r
  echo "=== CPU ==="; lscpu | head -20
  echo "=== RAM ==="; free -h
  echo "=== Disque ==="; lsblk -f; df -hT
  echo "=== USB ==="; lsusb
  echo "=== PCI ==="; lspci
  echo "=== Watchdog ==="; sudo wdctl 2>&1 || true
  echo "=== Réseau ==="; ip a; ip r
  echo "=== Hostname ==="; hostname; cat /etc/hostname
  echo "=== ModemManager ==="; systemctl is-active ModemManager 2>&1 || true
'
```

Output capturé textuellement ci-dessous (à remplir).

> _(Sera rempli par la session d'install.)_

### 2.3. Désactivation préventive de ModemManager

La SIM7600 est branchée. ModemManager (installé par défaut sur Debian) va confisquer les
ports `/dev/ttyUSBx` et empêcher la gateway custom de fonctionner ensuite. On le neutralise
maintenant, même si on ne configure pas la SIM7600 dans cette phase.

```bash
sudo systemctl mask --now ModemManager
sudo systemctl status ModemManager  # doit afficher "masked"
```

---

## 3. Hardening SSH

### 3.1. Vérification clé déployée

La clé publique a déjà été déployée en §2.1. Vérifier qu'on peut se connecter par clé sans
mot de passe **avant** de désactiver password auth.

### 3.2. sshd hardening

Fichier `/etc/ssh/sshd_config.d/99-hardening.conf` (déployé depuis
`infra/onsite/ssh/99-hardening.conf`) :

```
PasswordAuthentication no
PermitRootLogin no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
```

**Procédure de validation** : ouvrir une **2e session SSH par clé en parallèle** avant
`systemctl reload ssh`, pour ne pas se locker dehors si la conf est cassée.

### 3.3. Sudoers

`alarm` doit être membre du groupe `sudo`. Vérification :
```bash
groups alarm
```
Si absent : `sudo usermod -aG sudo alarm` (et reconnexion).

NOPASSWD limité à une whitelist dans `/etc/sudoers.d/alarm-ops` pour les commandes
healthcheck/deploy uniquement (à définir au moment où on installera ces services).

---

## 4. Pare-feu UFW

```bash
sudo apt install -y ufw
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow from 172.16.0.0/16 to any port 22 proto tcp comment 'SSH LAN site'
sudo ufw allow 51820/udp comment 'Wireguard'
sudo ufw allow from 172.16.0.0/16 to any port 8000 proto tcp comment 'Backend LAN site (apps Android)'
sudo ufw enable
sudo ufw status verbose
```

**À durcir** (cf. §13 Améliorations) : restreindre SSH aux IPs admin (au lieu du `/16` complet)
une fois la liste des postes admin figée.

Tout le trafic Patroni/etcd/replication passe par Wireguard — **aucun port ouvert en LAN
direct pour ces services**.

---

## 5. fail2ban (permissif pour install, à durcir avant prod)

```bash
sudo apt install -y fail2ban
```

Config `/etc/fail2ban/jail.d/sshd.local` (déployée depuis `infra/onsite/fail2ban/sshd.local`) :

```
[sshd]
enabled = true
maxretry = 100
findtime = 10m
bantime = 1h
```

**Permissif volontairement** pour la phase d'install : on itère sur la conf SSH/UFW et
on évite de se locker dehors. **Avant la mise en prod réelle**, durcir :
`maxretry=3`, `bantime=24h`, jail `recidive` activée. Cf. §13 Améliorations.

---

## 6. unattended-upgrades (security only, sans reboot auto)

Config dans `/etc/apt/apt.conf.d/52unattended-upgrades-local` (depuis
`infra/onsite/apt.conf.d/52unattended-upgrades-local`) :

```
Unattended-Upgrade::Origins-Pattern {
    "origin=Debian,codename=${distro_codename},label=Debian-Security";
};
Unattended-Upgrade::Automatic-Reboot "false";
Unattended-Upgrade::Mail "direction_technique@charlesmurgat.com";
Unattended-Upgrade::MailReport "on-change";
```

**Pas de reboot auto** : sinon les 2 nœuds on-site peuvent rebooter dans la même fenêtre →
quorum cluster perdu simultanément. Reboot reste manuel et coordonné via
`/usr/local/bin/coordinated-reboot.sh` (à créer plus tard) qui vérifie le quorum etcd avant
d'autoriser le reboot.

```bash
sudo unattended-upgrade --dry-run --debug | tail -30  # vérification
```

---

## 7. chrony (sync NTP)

Patroni/etcd font du consensus distribué : si l'horloge dérive de >2s entre nœuds, le leader
peut être considéré mort à tort → bascule inutile. Cible : <100ms.

```bash
sudo apt install -y chrony
sudo systemctl disable --now systemd-timesyncd 2>&1 || true  # conflit
```

Config `/etc/chrony/chrony.conf` (depuis `infra/onsite/chrony.conf`).

```bash
sudo systemctl restart chronyd
sleep 30
chronyc tracking  # offset doit être < 100ms
chronyc sources -v  # au moins une source ^*
```

---

## 8. journald persistant + rotation

```bash
sudo mkdir -p /var/log/journal
sudo systemd-tmpfiles --create --prefix /var/log/journal
```

Config `/etc/systemd/journald.conf.d/persistent.conf` (depuis
`infra/onsite/journald.conf.d/persistent.conf`).

```bash
sudo systemctl restart systemd-journald
journalctl --disk-usage  # doit être stable < 2G
journalctl --boot=-1 || echo "(pas encore de boot précédent)"
```

Rotation automatique par journald (`SystemMaxUse=2G` + `MaxFileSec=1week`). Pas de saturation
possible.

---

## 9. sysctl tuning Postgres

Config `/etc/sysctl.d/99-alarm-postgres.conf` (depuis
`infra/onsite/sysctl.d/99-alarm-postgres.conf`).

```bash
sudo sysctl --system  # applique
sysctl vm.swappiness  # doit afficher 1
```

---

## 10. Layout disque (LVM)

Cible :
- `/boot` ext4 hors LVM
- VG `alarm-vg` sur le reste, LVs séparés pour root / docker / pgdata / logs
- `noatime,nodiratime` partout
- 20%+ d'espace libre VG pour snapshots

**Si la machine est déjà installée sans LVM** : on documente l'écart en §1 État initial,
et on inscrit "Réévaluer le layout LVM au prochain reset" en §13 Améliorations. Pas de
repartitionnement live (trop risqué).

---

## 11. Watchdog matériel + applicatif

### Hardware watchdog

```bash
sudo wdctl  # doit lister un device
# Si absent : sudo modprobe softdog
```

Config `/etc/systemd/system.conf.d/watchdog.conf` (depuis
`infra/onsite/systemd/watchdog.conf`) :
```
[Manager]
RuntimeWatchdogSec=30s
RebootWatchdogSec=10min
```

### Applicatif : healthcheck systemd timer

Service `alarm-healthcheck.service` + timer toutes les 60s (depuis
`infra/onsite/systemd/`). Ping `localhost:8000/health`, log dans journald (traçable).
Si 5 fails consécutifs → `systemctl restart alarm-stack.service`.

> _Activé une fois le cluster en quorum._

---

## 12. SMART monitoring

```bash
sudo apt install -y smartmontools
sudo smartctl -a /dev/sda  # remplacer par le vrai device
sudo systemctl enable --now smartd
```

Config `/etc/smartd.conf` (depuis `infra/onsite/smartd.conf`).

---

## 13. Wireguard (mesh inter-nœuds)

```bash
sudo apt install -y wireguard
cd /etc/wireguard
sudo wg genkey | sudo tee privatekey | sudo wg pubkey | sudo tee publickey
sudo chmod 600 privatekey
```

Config `/etc/wireguard/wg0.conf` (depuis template `infra/onsite/wg0.conf.example` adapté).

**Onsite-2** : adresse `10.99.0.3/24`. Peers (placeholder tant que les autres nœuds n'existent
pas) :
- `10.99.0.1` (cloud / hors-site)
- `10.99.0.2` (onsite-1)

```bash
sudo systemctl enable --now wg-quick@wg0
sudo wg show
```

Status `no handshake yet` attendu tant que les autres nœuds n'existent pas.

---

## 14. Docker + Compose

Repo Docker officiel (les paquets Debian sont trop vieux) :

```bash
sudo apt install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/debian/gpg \
  -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
  https://download.docker.com/linux/debian $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

Logging driver global `/etc/docker/daemon.json` (depuis `infra/onsite/docker/daemon.json`) :
```json
{
  "log-driver": "json-file",
  "log-opts": {"max-size": "10m", "max-file": "3"}
}
```

```bash
sudo usermod -aG docker alarm  # alarm peut docker sans sudo
sudo systemctl restart docker
docker compose version  # ≥ 2.20
```

---

## 15. Cluster Patroni — bring-up mono-nœud

```bash
sudo mkdir -p /opt/alarm
sudo chown alarm:alarm /opt/alarm
sudo -u alarm git clone https://github.com/claude-murgat/Alarm2.0.git /opt/alarm
cd /opt/alarm
```

**Tentative as-is** (directive utilisateur "on ne modifie pas la conf, si ça marche pas
tu me dis") avec `.env.node1` actuel.

### Résultat constaté (2026-05-01 sur onsite-2)

```
docker compose --env-file .env.node1 -p node1 up --build -d
→ Build OK (images node1-patroni et node1-backend construites)
→ Container node1-etcd-1 démarre puis devient unhealthy
→ Patroni et backend ne démarrent jamais (depends_on: etcd healthy)
```

Logs etcd extraits :
```
"46d9b3abac6588fb is starting a new election at term 1"  (en boucle)
"sent MsgPreVote request to 31956d8c7d22439e"  (peer node2 = absent)
"sent MsgPreVote request to 4ac516d4d870fef4"  (peer node3 = absent)
"dial tcp 172.17.0.1:2382: i/o timeout"
"dial tcp 172.17.0.1:2384: i/o timeout"
"failed to publish local member to cluster through raft ... etcdserver: request timed out"
```

**Cause racine** : `.env.node1` déclare `ETCD_INITIAL_CLUSTER=node1=...,node2=...,node3=...`
avec les 3 endpoints sur `host.docker.internal:238x`. Avec le `ETCD_INITIAL_CLUSTER_STATE: new`
hardcodé dans `docker-compose.yml`, etcd attend une majorité (2/3) des membres pour élire
un leader. En mono-nœud, ne se forme jamais.

**Conclusion** : impossible de boot le cluster en mono-nœud avec la conf as-is (cf.
directive utilisateur "on ne modifie pas la conf"). À remonter à l'utilisateur :

| Option | Résultat |
|---|---|
| **A** — accepter une conf de bootstrap transitoire avec `ETCD_INITIAL_CLUSTER=node1=...` (1 seul membre) puis `etcdctl member add` quand les autres arrivent | Cluster up immédiatement, mais conf différente entre bootstrap et cible |
| **B** — attendre qu'un 2e nœud existe (provisionner onsite-1 ou cloud node) avant de démarrer le cluster | Conf strictement identique partout, mais aucun service tourne entre temps |
| **C** — utiliser `--force-new-cluster` au démarrage du 1er etcd, puis rejoindre les autres en `existing` | Délicat, peu documenté, risque de divergence |

**Recommandation** : Option B (attendre). Pour cette machine, on s'arrête au cluster
préparé mais désactivé. Les services `alarm-stack.service` et `alarm-healthcheck.service`
sont créés mais **désactivés** (`systemctl disable`) tant que le quorum n'est pas
atteignable.

### Cleanup post-essai

```bash
docker compose --env-file .env.node1 -p node1 down -v
```
Tous les containers, volumes et networks de l'essai ont été supprimés.

### Notes opérationnelles

- `backend/firebase-service-account.json` a été créé en placeholder JSON
  (`{"project_id":"placeholder-not-real"}`) pour permettre le mount Docker. **À remplacer**
  par le vrai fichier de credentials Firebase avant toute activation de prod.
- Le clone de `/opt/alarm` est sur `master` à l'heure du provisioning. Mise à jour
  ultérieure : `git -C /opt/alarm pull` (le service `alarm-stack` est désactivé donc pas
  de redémarrage automatique).

---

## 16. Backup PostgreSQL — préparation

> Activation différée jusqu'à formation du quorum cluster. Configuration documentée ici
> pour cohérence.

- Outil : pgBackRest (sidecar Docker)
- Stratégie : snapshots toutes les 6h, push via SSH/Wireguard sur les 2 autres nœuds
  (`/var/backups/alarm/pg/<source-node>/`)
- Retention : 7 jours par source × 3 sources = 21 snapshots à tout instant
- Chiffrement AES-256, passphrase dans `infra/onsite/secrets.yml` (hors-git)

Détails dans `infra/onsite/pgbackrest/README.md` (à créer en phase d'activation).

---

## 17. Monitoring local

> Activation différée. Documenté pour cohérence.

- `prometheus-node-exporter` exposé uniquement sur l'interface Wireguard
- Endpoint `/metrics` du backend (à vérifier)
- Alerting centralisé côté NODE3 cloud (Grafana/Alertmanager)

---

## 18. Vérification end-to-end (Phase 1)

| # | Test | Commande | Résultat attendu |
|---|---|---|---|
| 1 | SSH par clé | `ssh -i ~/.ssh/alarm_onsite_X alarm@<IP>` | Login sans mot de passe |
| 2 | Password auth refusé | `ssh -o PreferredAuthentications=password alarm@<IP>` | "Permission denied" |
| 3 | Root SSH refusé | `ssh root@<IP>` | "Permission denied" |
| 4 | UFW | `sudo ufw status verbose` | Seulement SSH + Wireguard + 8000 LAN |
| 5 | Time sync | `chronyc tracking` | Offset < 100ms |
| 6 | Sources NTP | `chronyc sources -v` | ≥ 1 source `^*` |
| 7 | Logs persistants | `journalctl --disk-usage` | < 2 Go, > 0 |
| 8 | Watchdog | `sudo wdctl` | Device présent et actif |
| 9 | SMART | `sudo smartctl -H /dev/sdX` | PASSED |
| 10 | Wireguard | `sudo wg show` | Interface up, peers listés |
| 11 | Docker | `docker run --rm hello-world` | OK |
| 12 | MAJ dry-run | `sudo unattended-upgrade --dry-run --debug` | Security uniquement, no reboot |
| 13 | ModemManager | `systemctl is-active ModemManager` | `inactive` ou `masked` |
| 14 | Reboot test | `sudo reboot` puis re-SSH | Machine remonte, services attendus actifs |

> Le cluster Patroni (health vert sur les 3 nœuds, replication lag, leader election) ne peut
> être validé qu'**après provisioning d'au moins un autre nœud**.

---

## 19. Maintenance régulière

### Mensuel
- Vérifier `chronyc tracking` (offset doit rester < 100ms)
- Vérifier `journalctl --disk-usage` (doit rester stable)
- Vérifier `smartctl -a` sur chaque SSD (Reallocated_Sector_Ct, Wear_Leveling)
- Vérifier `sudo unattended-upgrade --dry-run` (état des MAJ)
- Vérifier dernier snapshot pgBackRest sur les 2 autres nœuds

### Trimestriel
- Test de reboot coordonné (vérifier que la machine remonte clean)
- Test de restore d'un backup pgBackRest dans un cluster Patroni jetable + lancer la
  suite pytest contre (cf. backup tests dans le runner self-hosted GH Actions)
- Mettre à jour cette doc si quoi que ce soit a divergé

### Annuel
- Bump version Debian si LTS approche EOL
- Régénérer les clés SSH d'install si compromise suspectée
- Renouveler clés Wireguard

### Reboot coordonné
**Jamais** rebooter les 2 on-site dans la même fenêtre horaire (perte simultanée du quorum
côté on-site → cluster en panne). Toujours :
1. Vérifier quorum etcd : `etcdctl --endpoints=<peers> endpoint health`
2. Reboot de cette machine
3. Attendre re-formation du cluster (`patronictl list` montre tous les membres)
4. Seulement après → reboot de l'autre on-site si nécessaire

---

## 20. Améliorations à venir pour cette machine

> _Section vivante. Ajouter chaque trou identifié au fil des sessions._

### À faire avant mise en prod réelle
- [ ] **Durcir fail2ban** : passer `maxretry` de 100 à 3, `bantime` à 24h, activer la jail `recidive`
- [ ] **Restreindre SSH UFW** aux IPs admin spécifiques (au lieu du `172.16.0.0/16`)
- [ ] Confirmer la liste des IPs admin avec l'utilisateur

### À faire dès qu'un 2e nœud existe
- [ ] Échanger les clés Wireguard publiques avec les peers
- [ ] Activer `alarm-stack.service`
- [ ] Activer `alarm-healthcheck.service`
- [ ] Activer pgBackRest cross-node
- [ ] Valider le cluster Patroni (replication, failover)

### Trous identifiés à compenser ultérieurement
- [ ] **Phase 2 — Waveshare SIM7600** : udev rules, ModemManager off (déjà fait), gateway Python, mode ECM 4G
- [ ] **Phase 3 — CD V1** : build GHCR, promotion `:stable`, pull-based via systemd timer, canary order, auto-rollback
- [ ] **Phase 4 — CD V2** : auto-fix prod via bot IA (après ≥10 PRs propres consécutives du bot)
- [ ] Layout LVM si la machine n'a pas été installée avec (réévaluer au prochain reset)
- [ ] Console série / IPMI / out-of-band management — non disponible sur mini-PC business standard
- [ ] TPM hardware (si non présent) — sécurise les clés au boot

### Décisions différées
- [ ] DNS interne pour le mesh Wireguard : pas nécessaire à 3 nœuds, à reconsidérer au-delà
  ou si les IPs Wireguard changent souvent
- [ ] Endpoint Wireguard public pour NODE3 cloud (DDNS / IP fixe) — à choisir avec
  l'hébergeur cloud

---

## 21. Procédure de clone vers la 2e machine on-site

Suivre cette doc en remplaçant les valeurs marquées `<!-- onsite-2 -->` par les valeurs
de la nouvelle machine. Spécifiquement :

- IP machine, hostname (`onsite-2.alarm.local` → `onsite-1.alarm.local`)
- IP Wireguard (`10.99.0.3` → `10.99.0.2`)
- Clé SSH dédiée `~/.ssh/alarm_onsite_2` → `~/.ssh/alarm_onsite_1`
- Clés Wireguard à régénérer
- `alarm-bot.private-key.pem` (si runner self-hosted déployé sur cette machine — non en V1)

Tout le reste (fichiers `infra/onsite/*`) est strictement réutilisable sans modification.

---

## 22. Résultat de la session de provisioning onsite-2 (2026-05-01)

### Vérification end-to-end post-reboot (machine remontée seule, OK)

| # | Test | Résultat |
|---|---|---|
| 1 | SSH par clé | ✅ `key auth still works as alarm` |
| 2 | Password auth refusé | ✅ `Permission denied (publickey)` |
| 3 | Root SSH refusé | ✅ `Permission denied (publickey)` |
| 4 | UFW active | ✅ Status active, 4 règles attendues |
| 5 | Time sync (chrony) | ✅ stratum 5, offset 406 µs, "Normal" |
| 6 | Logs persistants | ✅ 26,5 Mo dans `/var/log/journal` (< 2 Go) |
| 7 | Hardware watchdog | ✅ iTCO_wdt actif, timeout 30s |
| 8 | Systemd watchdog | ✅ RuntimeWatchdogUSec=30s, RebootWatchdog=10min |
| 9 | SMART | ✅ PASSED sur `/dev/sda` |
| 10 | Wireguard | ✅ wg0 up sur `10.99.0.3/24`, port 51820, listening |
| 11 | Docker + Compose | ✅ Docker 29.4.2 + Compose v5.1.3 |
| 12 | DNS résolution | ✅ Cloudflare + Quad9, getent OK |
| 13 | ModemManager | ✅ inactive + masked |
| 14 | systemd-timesyncd | ✅ inactive (remplacé par chrony) |
| 15 | fail2ban sshd jail | ✅ active, 0 ban |
| 16 | unattended-upgrades dry-run | ✅ ne propose que security updates, pas de reboot |
| 17 | Hostname | ✅ `onsite-2.alarm.local` (static + pretty) |
| 18 | Reboot test | ✅ Machine remonte seule, tous services up automatiquement |

### Pubkey Wireguard à partager avec les futurs peers

```
onsite-2 (10.99.0.3) : IdbW+fqdOhFPSIO6XRp5oego+U/shypsOllOjYHQhi8=
```

Conservée également dans `infra/onsite/peers.md`.

### Cluster Patroni

**Bring-up tenté mais échoué** comme prévu (cf. §15) : la conf `.env.node1` actuelle déclare 3
membres etcd, donc en mono-nœud le quorum ne se forme pas. À remonter à l'utilisateur :
choix entre options A (bootstrap transitoire), B (attendre 2e nœud), C (force-new-cluster).

`alarm-stack.service` non créé pour l'instant (sera créé en même temps qu'on choisit
l'option de bootstrap).

### Points en attente avant prod

Voir §20 "Améliorations à venir".

### Sécurité du provisioning

- Mot de passe initial du user `alarm` utilisé une fois pour déployer la clé SSH `~/.ssh/alarm_onsite_2`.
- Ce mot de passe **ne doit pas être archivé** : changer si suspicion ou avant prod (`passwd alarm`).
- Aucun mot de passe n'a été écrit dans un fichier sur la cible ni dans le repo.
- Aucun NOPASSWD sudo n'a été créé : chaque commande sudo a fonctionné par stdin.

---

## 23. Historique des changements

- **2026-05-01** : création initiale + provisioning machine onsite-2 à 172.16.1.120
  - Phase 1 OS/réseau/Docker complète
  - Cluster bring-up tenté → blocage etcd attendu (3 membres déclarés en mono-nœud)
  - Reboot test passé
