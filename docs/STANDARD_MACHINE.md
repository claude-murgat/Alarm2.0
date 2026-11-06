# Machine standard sur site — Alarme Murgat

> **Référence unique** de la machine sur site (matériel, BIOS, installation, réseau, services) et des
> gestes qui la produisent. Demande du propriétaire du 2026-11-06 : standardiser les machines sur site
> et les processus.
>
> - Valeurs attendues : **un seul fichier**, [`infra/onsite/standard.env`](../infra/onsite/standard.env),
>   contrôlées **chaque jour** sur chaque machine par `alarm-node-check` (INV-186, §7).
> - Gestes : [`scripts/provision/`](../scripts/provision/README.md), dans l'ordre du §8.
> - **Changer le standard** = une PR qui modifie ce document **et** `standard.env` ensemble.
> - Historique et détails des premières installations (onsite-1, onsite-2, NODE3) :
>   [`PROVISIONING_ONSITE.md`](PROVISIONING_ONSITE.md). En cas de contradiction, **ce document prime**.

---

## 1. Matériel

| Élément | Standard |
|---|---|
| Modèle | **Dell OptiPlex 9020M** (format micro) |
| Parc au 2026-11-06 | alarm-1, alarm-2, alarm-3, alarm-4 (tous à Beaufort) |
| Carte réseau | intégrée Intel, interface **`eno1`** |
| USB | modem SIM7600 et Arduino UNO R4 (contact sec) branchables sur **n'importe quelle** machine (INV-182) ; **un seul modem par site** |
| RAM, disque | non relevés à l'inventaire du 2026-11-06 : à noter ici au prochain passage (`free -h`, `lsblk`) |

Hors standard : onsite-1 et onsite-2 (Shuttle NC03U, Core i5-7400T ; en retrait au déménagement,
INV-161) et NODE3 (VPS OVH). `alarm-node-check` ne les contrôle pas (§7).

---

## 2. BIOS (firmware)

### 2.1. Version standard : A19

| Champ | Valeur |
|---|---|
| Version | **A19** (dernière publiée par Dell pour l'OptiPlex 9020M) |
| Source | catalogue officiel Dell `downloads.dell.com/catalog/CatalogPC.cab` |
| Fichier | `FOLDER05650490M/1/9020MA19.exe` (sur `downloads.dell.com`) |
| Date | 19 juillet 2019 |
| Taille | 9 874 896 octets |
| MD5 (catalogue Dell) | `227f1be2dfcab777d498c02f95dbce28` |
| SHA-256 | `6ead68a5423ef181492fee85e0e4c667a67465d40b7a2fbbb4bdf7a78c548a7d` |
| Criticité Dell | Urgent |

État au 2026-11-06 : alarm-1 **A08**, alarm-2 **A04**, alarm-3 A19, alarm-4 A19.

### 2.2. Procédure de mise à jour

Une machine à la fois. Les 4 machines sont des nœuds **non votants** : le cluster ne perd pas son
quorum, mais la machine est hors service pendant le flash (passerelle comprise si elle porte le modem).

1. Télécharger `9020MA19.exe` depuis `downloads.dell.com` (chemin ci-dessus), le copier sur la machine.
2. Le déposer sur la partition EFI, après vérification de la taille et du SHA-256 :
   ```bash
   sudo bash /opt/alarm/scripts/provision/80-bios-stage.sh /tmp/9020MA19.exe
   ```
   Le script refuse un fichier dont l'empreinte diffère, et ne fait rien si le BIOS est déjà en A19.
   Il dépose `/boot/efi/9020MA19.exe` ; **rien n'est flashé** à distance.
3. **Sur place** : redémarrer, **F12** au logo Dell > **BIOS Flash Update** > choisir `9020MA19.exe`
   sur la partition EFI > lancer. **Ne jamais couper le courant** pendant le flash (plusieurs
   minutes, plusieurs redémarrages) : une coupure peut rendre la carte mère inutilisable.
4. Après le flash : repasser la checklist du §3 (un flash peut remettre des réglages à leur valeur
   par défaut), puis `sudo rm /boot/efi/9020MA19.exe` et `sudo alarm-node-check`.

> Non vérifié : un saut direct depuis A04 ou A08 vers A19. Si l'outil de flash le refuse, lire son
> message (Dell impose parfois une version intermédiaire) et ne rien forcer.

---

## 3. Réglages du BIOS (checklist sur place, F2)

À appliquer sur chaque machine, puis après chaque flash. Menus du BIOS Dell de l'OptiPlex 9020M.

| Réglage | Menu | Valeur | Pourquoi | Vu par `alarm-node-check` |
|---|---|---|---|---|
| Boot List Option | General > Boot Sequence | **UEFI** | Debian est installé en UEFI seulement : en Legacy la machine ne redémarre pas seule (alarm-2, §9.1) | oui (`demarrage`, `entrees_legacy`) |
| Enable Legacy Option ROMs | General > Advanced Boot Options | **décoché** | supprime les entrées legacy ; requis pour Secure Boot | oui (`entrees_legacy`) |
| Ordre de démarrage | General > Boot Sequence | **debian** en premier | pas de tentative sur une entrée morte | oui (`premier_demarrage`) |
| Secure Boot Enable | Secure Boot | **Enabled** | voir ci-dessous | oui (`secure_boot`) |
| AC Recovery | Power Management | **Power On** | la machine redémarre seule après une coupure de courant | **non** : vérifier sur place |
| Wake on LAN | Power Management | **LAN Only** | rallumage à distance d'une machine éteinte | **non** : vérifier sur place |
| Deep Sleep Control | Power Management | **Disabled** | sinon le Wake on LAN ne marche pas machine éteinte (à confirmer sur place) | non |
| Admin Password | Security | **défini** | personne ne change ces réglages sans lui | **non** |

**Mot de passe administrateur du BIOS** : défini sur chaque machine et conservé **dans le
gestionnaire de mots de passe du propriétaire** (Bitwarden). Il n'est **jamais** écrit dans ce dépôt,
dans un script, un ticket ou un message.

**Secure Boot activé : décision du propriétaire du 2026-11-06, réversible.** Les paquets signés
(`shim-signed`, `grub-efi-amd64-signed`, noyau Debian signé) sont installés partout, et alarm-3
démarre déjà avec Secure Boot actif. Revenir en arrière : F2 > Secure Boot > Secure Boot Enable
décoché, sans réinstallation, puis `STD_SECURE_BOOT=disabled` dans `standard.env` (PR). Aucun module
noyau hors Debian n'est utilisé (WireGuard est dans le noyau) ; un module DKMS non signé ne se
chargerait pas.

**Test de l'AC Recovery** (une fois par machine, cluster sain, sans alarme active) : débrancher le
secteur 10 s, rebrancher. La machine doit redémarrer seule et revenir dans le cluster (§9.5).

---

## 4. Installation de Debian 13

| Choix | Valeur |
|---|---|
| Média | `debian-13.x-amd64-netinst.iso` sur clé USB, démarrée **en UEFI** (F12 > entrée « UEFI: <clé> ») |
| Secure Boot | peut rester activé : l'installeur amd64 est signé |
| Nom de machine | `alarm-N` ; domaine : vide |
| Réseau | configuration **manuelle** : IP fixe du plan (§5), masque, passerelle = la box, DNS = la box |
| Mot de passe root | **vide** : root désactivé, l'utilisateur créé reçoit `sudo` |
| Utilisateur | `alarm` ; mot de passe dans le gestionnaire de mots de passe du propriétaire |
| Partitionnement | assisté, disque entier, **tout dans une seule partition**, **sans LVM** |
| Résultat attendu | GPT : `sda1` vfat 976 Mio `/boot/efi` (ESP), `sda2` ext4 `/`, `sda3` swap |
| Logiciels | **décocher** environnement de bureau et GNOME ; **cocher** « serveur SSH » et « utilitaires usuels du système » |

Après l'installation : `/etc/network/interfaces` contient `iface eno1 inet static` (ifupdown, pas de
NetworkManager) ; noyau 6.12. Pas de LVM : décision du standard, le §10 de `PROVISIONING_ONSITE.md`
(LVM) ne s'applique plus.

---

## 5. Réseau, noms et adresses

### 5.1. Plan (au 2026-11-06)

| Machine | Nœud | IP LAN | Passerelle | IP WireGuard | `GATEWAY_SITE` (site affiché) | Lieu physique | Rôle etcd |
|---|---|---|---|---|---|---|---|
| alarm-1 | node4 | 172.16.1.118/16 | 172.16.0.1 | 10.99.0.4 | `beaufort` | Beaufort | non votant |
| alarm-2 | node5 | 172.16.1.119/16 | 172.16.0.1 | 10.99.0.5 | `beaufort` | Beaufort | non votant |
| alarm-3 | node6 | 172.16.1.120/16 | 172.16.0.1 | 10.99.0.6 | `izeron` | Beaufort jusqu'au déménagement | non votant (voteur le jour du déménagement, INV-090) |
| alarm-4 | node7 | 172.16.1.121/16 | 172.16.0.1 | 10.99.0.7 | `izeron` | Beaufort jusqu'au déménagement | non votant (idem) |

Hors standard, pour mémoire : onsite-1 (node1, 192.168.1.50, 10.99.0.2) et onsite-2 (node2,
192.168.1.51, 10.99.0.3) à Izeron, voteurs ; NODE3 (node3, 51.210.105.102, 10.99.0.1), voteur et
relais WireGuard. Le plan machine → nœud → IP WireGuard → site est la variable `STD_PLAN` de
`standard.env` (les scripts de provisioning refusent une machine qui n'y est pas). Les IP LAN
changent avec le site (déménagement) : elles ne sont qu'ici et dans `/etc/network/interfaces`.

### 5.2. Réglages réseau

- **IP fixe** par ifupdown (`/etc/network/interfaces`, `iface eno1 inet static`, `address` en
  `/16` à Beaufort). Changer d'adresse : `60-change-ip.sh` (§9.3).
- **DNS** : `/etc/resolv.conf` = la box d'abord, puis `1.1.1.1` et `9.9.9.9` (INV-181, posés par
  `install.sh`).
- **WireGuard** `wg0` : `10.99.0.N/24`, port 51820, MTU 1280, clé privée
  `/etc/wireguard/prod-privatekey`, **NODE3 seul pair** (`51.210.105.102:51820`,
  `AllowedIPs = 10.99.0.0/24`, `PersistentKeepalive = 25`) : tout passe par le relais NODE3, rien à
  retoucher quand la machine change de LAN ou de site. Écart historique toléré : alarm-1 et alarm-2
  ont en plus un pair LAN direct l'un vers l'autre (piège §9.3). Clés publiques :
  [`infra/onsite/peers.md`](../infra/onsite/peers.md).
- **ufw** actif, entrée refusée par défaut ; ouverts : SSH (22) et backend (8000) depuis le LAN du
  site, SSH depuis `10.99.0.0/24` (orchestrateur CD), WireGuard 51820/udp, tout sur `wg0`.

### 5.3. Utilisateurs

| Utilisateur | Rôle |
|---|---|
| `alarm` | administration : groupes `sudo` et `docker` ; propriétaire de `/opt/alarm` ; SSH par clé (mot de passe coupé par `10-harden.sh --ssh`) ; clé de l'orchestrateur CD de NODE3 dans `authorized_keys` |
| `alarm-gateway` | système, sans shell, groupe `dialout` : passerelle SMS/appels/contact sec (créé par `install.sh`) |

---

## 6. Logiciels et services

| Composant | Standard | Posé par |
|---|---|---|
| Docker CE | dépôt `download.docker.com` (trixie) ; `/etc/docker/daemon.json` : `json-file`, 10 Mo × 3 | `00-base.sh` |
| Dépôt | `/opt/alarm` (clone git, propriétaire `alarm`), aligné sur le commit `:stable` par `alarm-host-sync` | à la main puis CD |
| ModemManager | **masqué** (il prendrait le port du SIM7600) | `10-harden.sh`, `install.sh` |
| ufw, fail2ban | actifs (`infra/onsite/fail2ban/sshd.local`) | `10-harden.sh` |
| Mises à jour | `unattended-upgrades`, sécurité seulement, **sans redémarrage automatique** | `10-harden.sh` |
| Heure | `chrony` (`infra/onsite/chrony.conf`), `systemd-timesyncd` désactivé | `10-harden.sh` |
| Journal | persistant (`/var/log/journal`, 2 Go max) | `10-harden.sh` |
| sysctl | `infra/onsite/sysctl.d/99-alarm-postgres.conf` | `10-harden.sh` |
| Watchdog | systemd `RuntimeWatchdogSec=30s`, `RebootWatchdogSec=10min` | `10-harden.sh` |
| SMART | `smartd` | `10-harden.sh` |
| Pull des images | `alarm-cd-pull.timer` | `10-harden.sh` |
| Pile hôte | `alarm-sms-gateway`, `alarm-modem-watchdog`, `alarm-4g-standby`, `alarm-wan-failover`, `alarm-modem-rat` ; timers `alarm-host-sync`, `alarm-gateway-sync`, `alarm-disk-check`, `alarm-node-check` | `infra/onsite/install.sh` (puis `alarm-host-sync`) |
| Cluster | Patroni + backend en non votant (`start-prod-node.sh --pull N`) | `40-join.sh` |
| Passerelle | `/etc/alarm-gateway.env` : `GATEWAY_KEY` (secret), `GATEWAY_SITE` du plan, `DRY_CONTACT_SOURCE=host` | `install.sh` + `50-gateway.sh` |

Units qui doivent être `enabled` : `STD_UNITS` de `standard.env`.

---

## 7. Contrôle de conformité (INV-186)

```bash
sudo alarm-node-check                               # à la demande
journalctl -u alarm-node-check -p warning --since -2d   # écarts des derniers contrôles quotidiens
```

Chaque jour vers 06:00 (rattrapé au démarrage si la machine était éteinte), une ligne par contrôle :
`OK|KO|INFO <contrôle> <détail>`, code 1 s'il y a un KO. Lignes KO au niveau **warning** du journal ;
un KO laisse `alarm-node-check.service` en `failed`. Lecture seule : il ne corrige rien.

Contrôlé : modèle et BIOS, UEFI, entrées legacy, Secure Boot, première entrée de démarrage, Debian 13,
`dpkg --audit`, DNS de secours, ufw, chrony, watchdog systemd, journal persistant, ModemManager,
Docker, `wg0` (adresse du plan, poignée de main NODE3 < 5 min), clé/site/contact sec de la
passerelle (la clé n'est jamais affichée), units `enabled`. **Non contrôlable depuis Linux** : AC
Recovery, Wake on LAN, mot de passe du BIOS (§3, sur place).

KO attendus au premier passage (inventaire du 2026-11-06) : `bios` sur alarm-1 et alarm-2,
`secure_boot` sur alarm-1, alarm-2 et alarm-4, jusqu'aux gestes sur place.

---

## 8. Mettre en service une nouvelle machine

Ordre standard (détail, et où chaque script tourne : [`scripts/provision/README.md`](../scripts/provision/README.md)) :

1. Ajouter la machine au plan (`STD_PLAN` de `standard.env`) et ses `.env.prod.nodeN` (PR).
2. BIOS : flash A19 si besoin (§2), réglages (§3).
3. Debian 13 (§4) ; déposer la clé SSH du poste (`ssh-copy-id`), cloner `/opt/alarm`.
4. `00-base.sh`, `10-harden.sh`, `sudo bash infra/onsite/install.sh`.
5. WireGuard : `20-wireguard-node.sh` (machine), `21-wireguard-node3.sh` (NODE3), `20` à nouveau.
6. `30-secrets.sh` (poste), `40-join.sh`, `50-gateway.sh`, `10-harden.sh --ssh`.
7. `sudo alarm-node-check` : **que des OK** avant de déclarer la machine en service.

Remplacer un voteur : [`REPLACE_ONSITE_NODE.md`](REPLACE_ONSITE_NODE.md).

---

## 9. Pièges connus

### 9.1. Boot List Option en Legacy : la machine ne redémarre pas seule
**alarm-2, 2026-11-06.** Debian est installé en UEFI seulement (pas de GRUB legacy) ; avec Boot List
Option = Legacy, le BIOS ne trouve rien à démarrer et la machine reste à l'écran du firmware après un
redémarrage. Correctif sur place : F2 > General > Boot Sequence > Boot List Option = **UEFI**, et
General > Advanced Boot Options > **décocher** Enable Legacy Option ROMs. Détecté par
`alarm-node-check` (`demarrage`, `entrees_legacy`).

### 9.2. Entrée NVRAM « Windows Boot Manager » orpheline
**alarm-2, 2026-11-06.** Une entrée de démarrage du Windows d'origine (disque effacé depuis) restait
en NVRAM. `90-uefi-repair.sh` retire chaque entrée « Windows Boot Manager » dont la partition
(PARTUUID) n'existe plus, termine les paquets inachevés, réinstalle GRUB (avec le chemin de secours
`EFI/BOOT/BOOTX64.EFI`) et `update-grub`. Détecté par `alarm-node-check` (`premier_demarrage`,
`dpkg_audit`).

### 9.3. Échange d'IP ou de câbles : endpoints WireGuard LAN périmés
alarm-1 et alarm-2 ont un pair WireGuard LAN direct l'un vers l'autre (`Endpoint = 172.16.1.x:51820`).
Après un changement d'adresse (alarm-1 passée de .120 à .118 le 2026-11-04, puis un échange de câbles
alarm-2 ↔ alarm-3), ce pair visait toujours l'ancienne adresse, désormais celle d'**une autre
machine** : le lien direct n'est plus fiable (le relais NODE3, lui, suit tout seul par le roaming
WireGuard). Après tout changement d'IP LAN :
- changer l'adresse avec `60-change-ip.sh` (refuse une adresse qui répond déjà : conflit) ; une machine
  dont l'IPv4 est en conflit reste joignable par son IPv6 lien-local (`ssh alarm@fe80::…%<interface>`) ;
- sur l'autre machine, repointer `Endpoint =` du pair LAN puis `sudo wg syncconf wg0 <(wg-quick strip wg0)` ;
- sur le poste : `ssh-keygen -R <ancienne IP>` ; nouveau LAN : relancer `10-harden.sh` (ufw).
Le standard (`20-wireguard-node.sh`) n'a **pas** de pair LAN : NODE3 seul.

### 9.4. Modem non détecté après un déplacement
Le modem prend le site de la machine où il est branché (INV-182). Si la passerelle de la machine
d'arrivée reste « En attente du modem » au-delà de 15 s :
- `lsusb | grep -i 1e0e` : le modem est-il vu ? sinon câble, port, alimentation ;
- `MODEM_AT_PORT=/dev/sim7600_at` dans `/etc/alarm-gateway.env` : ce lien udev peut viser autre chose
  sur la nouvelle machine ; retirer la ligne (la détection automatique suffit) ;
- `CNMP=2` n'est réappliqué qu'au démarrage : `sudo systemctl restart alarm-modem-rat` ;
- `MODEM_XHCI` (contrôleur USB, défaut `0000:00:14.0`) : vérifier `readlink -f /sys/bus/usb/devices/usb1`
  (non vérifié sur l'OptiPlex 9020M) ;
- `GATEWAY_SITE` de la machine d'arrivée, et **un seul modem par site**.
Sur la machine de départ : **une** récupération USB et **un** email « Modem DOWN », attendus (INV-184).

### 9.5. Replicas bloqués après le redémarrage d'un site
**2026-11-06** : redémarrage des 4 machines de Beaufort pendant un déploiement ; node4, node6 et node7
sont revenus `running` (WAL déjà recyclés par le leader) au lieu de `streaming`. Depuis INV-168 le
leader les **re-clone seul, un par un**, après 10 min de blocage chacun (il suffit qu'un replica sain
innocente le leader). Rien à faire : suivre `patronictl list` (onglet Cluster du web). `patronictl
reinit` à la main seulement si rien ne bouge (cluster en pause, aucun replica sain).

---

## 10. Historique

- **2026-11-06** : création (demande du propriétaire). Inventaire : 4 × OptiPlex 9020M, BIOS A08 / A04
  / A19 / A19, Secure Boot sur alarm-3 seulement ; incident alarm-2 (Legacy + entrée Windows) ; standard
  BIOS A19, Secure Boot activé ; contrôle quotidien `alarm-node-check` (INV-186) ; scripts de
  provisioning versionnés (`scripts/provision/`).
