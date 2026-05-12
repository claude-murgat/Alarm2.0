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

NOPASSWD limité à une whitelist dans `/etc/sudoers.d/alarm-cd` pour les commandes
du flux Continuous Deployment V1 (cf docs/CD_DESIGN.md §3 et brief Phase 3) :
`install` des fichiers `alarm-cd-pull.{sh,service,timer}`, `systemctl` sur les units
correspondantes, `journalctl -u alarm-cd-pull*`. Voir
[`infra/onsite/sudoers/alarm-cd`](../infra/onsite/sudoers/alarm-cd) pour la liste
exacte.

Déploiement (à faire **une fois par nœud onsite**, requiert sudo interactif) :
```bash
# Ouvrir d'abord une 2e session SSH en filet de securite (cf §3.2), puis :
sudo install -m 440 -o root -g root \
  /opt/alarm/infra/onsite/sudoers/alarm-cd \
  /etc/sudoers.d/alarm-cd
sudo visudo -c    # doit afficher "parsed OK"
# Test non-interactif depuis poste d'admin :
ssh alarm@<noeud> "sudo -n systemctl status alarm-cd-pull.timer"
```

> NODE3 cloud OVH a historiquement `(ALL) NOPASSWD: ALL` (heritage du provisioning
> cloud, cf §22ter). À aligner sur la whitelist `alarm-cd` à terme — ouvert dans
> les "Points en attente avant prod" du §22ter.

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

**Recommandation initiale (2026-05-01)** : Option B (attendre). Pour cette machine,
on s'arrête au cluster préparé mais désactivé.

> **Mise à jour 2026-05-09 — résolution effective : option D (cf §22bis).**
> En arrivant à 3 nœuds (onsite-2 + onsite-1 + NODE3 cloud) avec mesh Wireguard
> opérationnel, l'analyse a montré que les 3 options A/B/C supposaient implicitement
> que le `docker-compose.yml` était cross-machine — ce qui n'était pas le cas (la
> conf utilisait `host.docker.internal:238x`, donc mono-host). PR #51 a introduit
> l'**option D = variabilisation de `docker-compose.yml`** avec `${ADVERTISE_HOST:?…}`
> + fichiers `.env.prod.node{1,2,3}` qui injectent les IPs Wireguard `10.99.0.{1,2,3}`.
> Bring-up cluster 3-way effectif le 2026-05-10 via `scripts/start-prod-node.sh`.
> Détails et adaptations dans **§22bis** (résultat onsite-1) et **§22ter** (NODE3 cloud).

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

## 22bis. Résultat de la session de provisioning onsite-1 (2026-05-06 + audit 2026-05-08)

### Specs constatées (machine onsite-1, 172.16.1.121)

| Élément | Valeur | Note |
|---|---|---|
| Hardware | Intel Core i5-7400T 4c/4t @ 2.4 GHz | Plus puissant que onsite-2 (Celeron 2c) |
| OS | Debian 13 (trixie) 13.4 | Identique onsite-2 |
| Kernel | 6.12.85+deb13-amd64 | OK |
| RAM | **8 Gio** (cible ≥ 8 Gio atteinte) | ✅ vs 4 Gio onsite-2 |
| Disque | NVMe 240 Go (210 Go libres), ext4 root + swap 7,9 Gio | ✅ vs SATA 28 Go onsite-2 |
| Réseau | `enp0s31f6 172.16.1.121/16`, `wlp2s0` (down), Bluetooth Intel | OK |
| Hostname | `onsite-1.alarm.local` (static + pretty) | ✅ |

### Vérification end-to-end (audit 2026-05-08)

| # | Test | Résultat |
|---|---|---|
| 1 | SSH par clé `alarm_onsite_1` | ✅ |
| 2 | Password auth refusé | ✅ `passwordauthentication no` effectif |
| 3 | Root SSH refusé | ✅ `permitrootlogin no` effectif |
| 4 | UFW active | ✅ 4 règles (SSH/22 LAN, WG/51820, Backend/8000 LAN, +IPv6 WG) |
| 5 | Time sync (chrony) | ✅ Stratum 2, offset **304 µs**, source `^*` 109.190.177.205 |
| 6 | Logs persistants | ✅ 11,5 Mo dans `/var/log/journal` (< 2 Go) |
| 7 | Hardware watchdog | ✅ iTCO_wdt actif, timeout 30s |
| 8 | Systemd watchdog | ✅ Runtime 30s, Reboot 10min |
| 9 | SMART NVMe | ✅ PASSED sur `/dev/nvme0n1` |
| 10 | Wireguard interface | ✅ wg0 up sur `10.99.0.2/24`, port 51820 |
| 11 | **Wireguard mesh fonctionnel** | ✅ ping `10.99.0.3` (onsite-2) = **0,7 ms** RTT |
| 12 | Docker hello-world | ✅ |
| 13 | ModemManager | ✅ inactive + masked |
| 14 | unattended-upgrade dry-run | ✅ rien à upgrader, security only |

### Pubkey Wireguard onsite-1

```
onsite-1 (10.99.0.2) : iO0HHo7Lbuvqs4rV6C456dSm8d+T3ef96CHA9m32CHE=
```

Ajoutée à `infra/onsite/peers.md`. Le peer `onsite-2` connaissait déjà `onsite-1` dans
sa config wg0.conf (handshake mutuel observé le 6 mai et confirmé le 8 mai par ping ICMP
sur le tunnel).

### Mesh Wireguard 3-way (complété le 2026-05-09)

NODE3 cloud (10.99.0.1, `51.210.105.102:50922`, provisionné le 2026-05-02) avait dans
sa config les peers onsite-1 et onsite-2 mais **les onsite n'avaient pas NODE3** dans
leur `wg0.conf`. Asymétrie corrigée le 2026-05-09 :

- Peer NODE3 (`PublicKey GDN64aY60tBSWKN4qA6GBd/JhistmDP2oF1qN0Xj9gw=`,
  `Endpoint 51.210.105.102:51820`, `PersistentKeepalive 25`) ajouté dans `wg0.conf`
  de `onsite-1` et `onsite-2`.
- `systemctl restart wg-quick@wg0` sur les 2 onsite, handshake établi en < 1 s.
- Mesh 3-way bidirectionnel confirmé par 5 ping ICMP croisés (RTT < 1 ms en LAN,
  16,6 ms vers cloud OVH).
- NAT site : les onsite sortent via `31.204.85.180` (router site), mappings ports
  dynamiques. NODE3 apprend les endpoints via PersistentKeepalive — pas d'`Endpoint =`
  déclaré côté NODE3 pour les onsite (correct).
- Tableau complet des handshakes dans `infra/onsite/peers.md`.

### Cluster Patroni

**Bring-up non tenté.** Mesh WG 3-way OK, mais nouveau blocage identifié le 2026-05-09 :
le `docker-compose.yml` actuel est conçu **mono-host** (les 3 instances etcd se voient
sur `host.docker.internal:238x` du même Docker daemon). Inutilisable tel quel pour un
cluster cross-machine via Wireguard.

Les options A/B/C de §15 reposaient toutes implicitement sur cette conf mono-host.
**Option D à introduire** : overlay `docker-compose.prod.yml` qui surcharge les
endpoints etcd avec les IPs Wireguard `10.99.0.{1,2,3}:2380`, sans toucher au fichier
de base utilisé par les tests CI mono-host. Sera fait dans une session/PR dédiée.

### Points en attente avant prod

Voir §20 "Améliorations à venir" — applicable identiquement à onsite-1.

### Sécurité du provisioning

- Le mdp initial du user `alarm` n'a **pas** été changé après le provisioning du 6 mai.
  Il a fait surface pendant la session d'audit du 8 mai. **À changer avant prod**
  (`passwd alarm`) avec un mdp fort (≥ 12 caractères aléatoires) stocké dans Bitwarden.
- Aucun NOPASSWD sudo n'est créé : `sudo` demande toujours le mdp.
- `/opt/alarm` est cloné (commit master du 6 mai) — `git pull` à faire avant
  toute activation des services.

---

## 22ter. Résultat de la session de provisioning NODE3 cloud (2026-05-09)

> **Cible** : NODE3 cloud OVH `51.210.105.102:50922` (port SSH custom OVH KB),
> WG `10.99.0.1`. **Différent des onsite** : VPS hébergé OVH, exposé Internet,
> rôle hors-site dans la topologie 2 onsite + 1 cloud (cf §0).
>
> Cette section documente le provisioning applicatif (Docker + clone repo) qui
> manquait, **les divergences observées vs procédure §3-§14 onsite, et les
> adaptations à conserver pour les futurs cloud nodes.**

### Specs constatées (machine NODE3, 51.210.105.102)

| Élément | Valeur | Note |
|---|---|---|
| Hardware | Intel Core (Haswell, no TSX) — VPS OVH, 4 vCPU | Pas d'accès au modèle physique |
| OS | Debian 13 (trixie) 13.x | Identique aux onsite |
| RAM | **7,6 Gio** | Cible ≥ 8 Gio quasi atteinte |
| Disque | **75 Go** ext4 single partition + EFI + biosboot | Pas de LVM (default OVH) |
| Réseau | IP publique fixe, pas de NAT côté serveur | Différent des onsite |
| Hostname | `vps-1560caaa` (default OVH) | **Pas changé** en `node3.alarm.local` (cf adaptations) |

### État avant cette session

Phase 1 OS/sécurité **déjà complète** (provisionnée hors-PR avant 2026-05-09) :
- ✅ §3 sshd hardening (`99-hardening.conf` présent, `port 50922`, `permitrootlogin no`)
- ✅ §4 UFW active avec règles cloud-spécifiques (cf adaptations)
- ✅ §5 fail2ban (jails `sshd` + `recidive`)
- ✅ §6 unattended-upgrades configuré
- ✅ §7 chrony (Stratum 2, offset < 100 µs)
- ✅ §8 journald persistent (32 Mo)
- ✅ §9 sysctl Postgres (swappiness=1, overcommit_memory=2, somaxconn=1024)
- ✅ §11 watchdog actif (cf adaptations — softdog au lieu de iTCO_wdt)
- ✅ §13 Wireguard up sur `10.99.0.1`, peers onsite-1 et onsite-2 dans `wg0.conf`

**Manquant** :
- ❌ §14 Docker + Compose
- ❌ git non installé
- ❌ `/opt/alarm` non cloné

### Actions de provisioning effectuées (2026-05-09)

1. `apt install git` (2.47.3-0+deb13u1)
2. Install Docker via repo officiel (procédure §14) :
   - `apt install ca-certificates curl`
   - Clé GPG Docker dans `/etc/apt/keyrings/docker.asc`
   - Repo `https://download.docker.com/linux/debian trixie stable`
   - `apt install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin`
   - Versions : Docker 29.4.3 + Compose v5.1.3
3. `/etc/docker/daemon.json` : logging json-file 10m × 3 fichiers (conforme §14)
4. `usermod -aG docker alarm` (alarm UID 1001, désormais dans `docker` GID 989)
5. `mkdir /opt/alarm && chown alarm:alarm` puis
   `sudo -u alarm git clone https://github.com/claude-murgat/Alarm2.0.git /opt/alarm`
   (HEAD `5cec8a4` = master post PR #45 + PR #51)
6. `firebase-service-account.json` placeholder `{"project_id":"placeholder-not-real"}`
   déposé dans `/opt/alarm/backend/`
7. Test `sudo -u alarm sg docker -c "docker run --rm hello-world"` ✅

### Adaptations cloud (OVH VPS) vs procédure onsite

> **À reproduire telles quelles pour tout futur nœud cloud OVH.** Ne sont PAS des
> oublis ou des écarts à corriger : ce sont des choix volontaires liés à la
> nature VPS-cloud-Internet de la machine.

| § procédure | Adaptation cloud | Raison |
|---|---|---|
| §3 sshd | `Port 50922` au lieu de 22 | Recommandation OVH KB anti-bruteforce |
| §3 sshd | `PasswordAuthentication yes` | **À confirmer** — probablement pour rescue OVH si toutes clés cassées. Si clé d'urgence stockée en Bitwarden suffit, durcir en `no` |
| §4 UFW | `8000/tcp ALLOW IN Anywhere` | Backend exposé Internet pour les apps Android (vs LAN-only sur onsite). UFW reste deny-incoming par défaut, seuls 50922/51820/8000 ouverts |
| §4 UFW | Pas de restriction `from 172.16.0.0/16` | Pas de LAN site, donc pas de subnet à whitelister |
| §10 LVM | Layout simple sda1 sans LVM | Default partitioning OVH, pas de snapshot disque possible (mais snapshot OVH dispo via panel) |
| §11 watchdog | `softdog` au lieu de `iTCO_wdt` | VPS sans accès au watchdog hardware ; l'hyperviseur OVH gère reboot auto si kernel bloqué |
| §12 SMART | smartd inactif | Storage virtuel, pas de SSD physique à monitorer ; OVH monitore côté hyperviseur |
| §2 SIM7600 | Non applicable | Pas de port USB ni rôle gateway SMS/voix sur le cloud |
| §6 reboot coordination | Identique mais OVH peut rebooter pour maintenance | Souscrire aux notifications OVH ; risque de perte simultanée du quorum si onsite reboote en même temps |
| Hostname | Default OVH `vps-XXXXXX` conservé | Changement non critique ; peut affecter reverse DNS si activé. Conservation sûre |
| §15 cluster | Identique avec `.env.prod.node3` | Variabilisation docker-compose (PR #51) supporte le cross-machine via WG |

### Vérification end-to-end (extrait des tests §18 applicables)

| # | Test | Résultat |
|---|---|---|
| 1 | SSH par clé `alarm_node3` sur port 50922 | ✅ |
| 2 | Password auth | ⚠️ `yes` (intentionnel cloud, cf adaptations §3 — à reconfirmer avec utilisateur) |
| 3 | Root SSH refusé | ✅ `permitrootlogin no` |
| 4 | UFW active | ✅ 4 règles cloud (50922/51820/8000 + wg0 mesh) |
| 5 | chrony tracking | ✅ Stratum 2, offset 96 µs |
| 6 | Logs persistants | ✅ 32 Mo dans `/var/log/journal` |
| 7 | Watchdog | ✅ softdog actif (timeout 30s) — cloud-adapté |
| 8 | Systemd watchdog | ✅ |
| 9 | SMART | N/A (storage virtuel — cloud-adapté) |
| 10 | Wireguard interface | ✅ wg0 up sur `10.99.0.1/24` |
| 11 | **Mesh 3-way** | ✅ ping `10.99.0.2` et `10.99.0.3` = 16,6 ms |
| 12 | **Docker hello-world** | ✅ (cette session) |
| 13 | ModemManager | N/A (pas de modem sur cloud) |
| 14 | unattended-upgrade dry-run | ✅ |

### Points en attente avant prod

- [ ] **Confirmer `PasswordAuthentication`** : reste à `yes` pour rescue, ou durcir à `no` ?
  Décision à prendre avec utilisateur. Dépend de la stratégie de fallback (Bitwarden vs rescue OVH).
- [ ] **Vrai `firebase-service-account.json`** à déposer sur les 3 machines avant
  activation des services (le placeholder est suffisant pour le bring-up etcd/Patroni
  mais le backend FCM échouera en runtime).
- [ ] **SMTP relay** : `SMTP_HOST=host.docker.internal` côté backend pointe sur le host,
  qui n'a pas de mailhog/postfix sur NODE3. Définir un relais SMTP réel (postfix → SES /
  Sendgrid / SMTP perso) avant prod, sinon les emails escalade ne partent pas.

### Sécurité du provisioning

- Le mdp `alarm` sur NODE3 a été utilisé pendant cette session (sudo). Il **ne quitte pas
  la VM provisioning** (jamais écrit en clair dans le repo).
- Aucun NOPASSWD sudo configuré.
- `/opt/alarm` cloné par `alarm` user, pas root → permissions correctes.

---

## 22quater. Gestion des secrets en prod (`.env.prod.secrets`)

> **Pattern à appliquer pour TOUTE valeur secrète injectée dans la stack.**
> Pas de hardcode dans `docker-compose.yml`, pas de commit dans `.env.prod.node*`
> (qui sont publics dans le repo).

### Principe

Le repo `Alarm2.0` est **public sur GitHub**. Tout fichier committé est lisible
de l'extérieur. Donc :

- **`docker-compose.yml`** : utilise `${VARIABLE:?explication}` pour les secrets.
  Le `:?` fait échouer compose si la variable n'est pas définie (fail-fast plutôt
  qu'un crypto signé avec une chaîne vide).
- **`.env.dev` / `.env.node{1,2,3}`** : valeurs **non-secrètes** uniquement
  (configuration mono-host pour CI/dev). Les SECRET_KEY committées ici sont
  marquées `ci-dev-key-public-not-secret-XXXX`.
- **`.env.prod.node{1,2,3}`** : configuration prod par-machine (IPs WG, ports,
  cluster bootstrap) **sans secrets**.
- **`.env.prod.secrets`** : GITIGNORED. Déposé à la main sur chaque machine
  dans `/opt/alarm/.env.prod.secrets` (mode `600`, owner `alarm`). Sourcé par
  `scripts/start-prod-node.sh` au bring-up et exporté vers l'env shell pour
  substitution dans le compose.

### Procédure de déploiement initiale (par machine)

```bash
# Sur la machine prod (onsite-1, onsite-2, ou NODE3 cloud) :
cd /opt/alarm
cp .env.prod.secrets.example .env.prod.secrets
# Editer pour remplacer les <PLACEHOLDER> par les vraies valeurs
# (a recuperer dans Bitwarden, NE PAS regenerer une nouvelle valeur differente
# entre les 3 machines — toutes les 3 doivent avoir la MEME SECRET_KEY).
nano .env.prod.secrets   # ou vim, etc.
chmod 600 .env.prod.secrets
chown alarm:alarm .env.prod.secrets
# Au bring-up suivant :
./scripts/start-prod-node.sh <1|2|3>
```

> **CRITIQUE** : les 3 machines doivent partager la **même** SECRET_KEY pour
> que les JWT signés par n'importe quel backend soient acceptés par les 2
> autres (rotation circulaire des apps Android côté `ApiClient.kt`).

### Procédure de rotation (sans downtime apparent)

1. **Génération nouvelle valeur** :
   ```bash
   openssl rand -hex 32
   ```
2. **Stockage Bitwarden** : note sécurisée "Alarme Murgat — SECRET_KEY YYYY-MM-DD".
3. **Déploiement coordonné sur les 3 machines** (idéalement <5 min entre elles
   pour minimiser la fenêtre d'invalidité de tokens) :
   ```bash
   sed -i.bak 's/^SECRET_KEY=.*/SECRET_KEY=<nouvelle valeur>/' /opt/alarm/.env.prod.secrets
   ./scripts/start-prod-node.sh <X>   # restart backend container avec nouveau env
   ```
4. **Conséquence** : tous les JWT existants (signés avec l'ancienne SECRET_KEY)
   sont invalidés → tous les utilisateurs doivent se reconnecter.
5. **Vérification** :
   ```bash
   curl http://10.99.0.1:8000/health   # secret_key_default doit rester false
   ```

### Liste des secrets actuellement gérés via `.env.prod.secrets`

| Variable | Usage | Conséquence si compromis |
|---|---|---|
| `SECRET_KEY` | Signature JWT (auth.py) | **Forge admin tokens à distance** via /api/auth/login → CRITIQUE |

### Liste des secrets **encore hardcodés** dans le repo (à migrer)

> _Section vivante — chaque migration produit une PR + une rotation coordonnée
> avec mise à jour de cette liste._

- [ ] `alarm_secret` (PG user `alarm`) dans `docker-compose.yml:71`
- [ ] `postgres_secret` (PG superuser) dans `patroni/patroni.yml:53`
- [ ] `rep_secret` (PG replication) dans `patroni/patroni.yml:56`
- [ ] Bootstrap users `admin`/`user1`/`user2` créés avec mdp hardcodés dans
  `backend/app/main.py:42-46` (les comptes existent en DB ; mdp **rotated en
  SQL le 2026-05-10**, mais le code crée encore `admin/admin123` etc. au prochain
  bootstrap d'une DB vide. Mitigation à long terme : remplacer par un script
  d'init `--first-boot` ou un endpoint admin gated)

---

## 23. Historique des changements

- **2026-05-01** : création initiale + provisioning machine onsite-2 à 172.16.1.120
  - Phase 1 OS/réseau/Docker complète
  - Cluster bring-up tenté → blocage etcd attendu (3 membres déclarés en mono-nœud)
  - Reboot test passé
- **2026-05-06** : provisioning machine onsite-1 à 172.16.1.121
  - Phase 1 OS/réseau/Docker complète, identique au standard
  - Wireguard configuré côté onsite-1 (10.99.0.2) avec peer onsite-2 (10.99.0.3)
- **2026-05-08** : audit end-to-end onsite-1 + MAJ peers.md
  - 14 tests §18 passants
  - Mesh Wireguard fonctionnel onsite-1 ↔ onsite-2 (RTT < 1 ms LAN)
  - Pubkey onsite-1 ajoutée à `peers.md`
- **2026-05-09** : complétion mesh Wireguard 3-way + blocage docker-compose identifié
  - Peer NODE3 (cloud OVH 51.210.105.102:51820, pubkey
    `GDN64aY60tBSWKN4qA6GBd/JhistmDP2oF1qN0Xj9gw=`) ajouté dans wg0.conf de
    onsite-1 et onsite-2 avec `PersistentKeepalive=25`
  - 5 handshakes mesh confirmés bidirectionnels (LAN < 1 ms, cloud 16,6 ms)
  - `peers.md` : NODE3 endpoint, table handshakes complète, note NAT site
  - Cluster bring-up reporté : `docker-compose.yml` mono-host, option D à faire
- **2026-05-09 (suite)** : option D implémentée par variabilisation (PR #51 mergée)
  - `docker-compose.yml` : 4 occurrences `host.docker.internal` → `${ADVERTISE_HOST:-host.docker.internal}`
  - Création `.env.prod.node{1,2,3}` avec ports uniformes 2379/2380/5432/8008/8000
    et endpoints en IPs Wireguard `10.99.0.{1,2,3}`
  - Création `scripts/start-prod-node.sh` (wrapper avec checks pré-bring-up)
  - Choix variabilisation vs overlay : single source of truth, rétrocompat par defaults
- **2026-05-09 (suite 2)** : provisioning applicatif NODE3 cloud (cf §22ter)
  - NODE3 avait Phase 1 OS/réseau/sécurité complète (sshd, ufw, fail2ban, chrony,
    journald, sysctl, watchdog soft, wireguard) **mais pas Docker/git/clone**
  - Action : `apt install git`, install Docker (repo officiel), `usermod -aG docker alarm`,
    `git clone https://github.com/claude-murgat/Alarm2.0.git /opt/alarm`,
    placeholder `firebase-service-account.json`, `daemon.json` logging conforme §14
  - `docker run hello-world` ✅ — NODE3 prêt pour bring-up cluster 3-way
- **2026-05-10** : bring-up cluster 3-way effectif + introduction §22quater (secrets hygiene)
  - `scripts/start-prod-node.sh {1,2,3}` lancés en parallèle sur les 3 machines :
    9 containers up (3 × etcd + patroni + backend), etcd 3-membres en quorum
    (leader RAFT = node1 onsite-1), Patroni `alarm-cluster` 1 leader + 2 replicas
    en streaming, replication lag = 0, 3 backends répondent /health
  - Note : self-loop Docker connu (chaque container etcd/patroni ne peut pas reach
    son propre host:port via WG IP — NAT bridge boucle). Non bloquant car les
    2 autres peers via WG suffisent au quorum. À résoudre dans une PR future via
    `network_mode: host` ou en mettant `127.0.0.1` en premier dans `PATRONI_ETCD3_HOSTS`
  - **PR #53 secrets hygiene** : `SECRET_KEY` sortie de `docker-compose.yml` (qui est
    publiquement accessible — repo GitHub public). Désormais sourcée via
    `.env.prod.secrets` gitignored, déposé à la main sur chaque machine
    (`chmod 600`, `chown alarm:alarm`), sourcé par `start-prod-node.sh` au bring-up
  - Introduction du pattern `.env.prod.secrets` (§22quater) à généraliser pour tous
    les secrets futurs (PG passwords, FCM, SMTP — listés en §22quater "secrets
    encore hardcodés à migrer")
