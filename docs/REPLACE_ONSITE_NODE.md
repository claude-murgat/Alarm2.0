# Remplacer un nœud sur site du cluster de prod

Remplacer onsite-1 ou onsite-2 par une nouvelle machine **sans interrompre le service**.
On remplace **un nœud à la fois** : etcd et Patroni gardent 2 membres sur 3 et les alarmes continuent.

Cible (décision du propriétaire du 2026-11-05 ; remplace le plan du 2026-10-22 où alarm-1/alarm-2 devaient remplacer onsite-1/onsite-2) :

| Machine | IP LAN | `NODE_NAME` | IP WireGuard | Site affiché (`node_locations`) | Rôle |
|---|---|---|---|---|---|
| onsite-1 | 192.168.1.50 | node1 | 10.99.0.2 | Izeron | voteur etcd + Patroni + backend — **bientôt obsolète**, gardé jusqu'au déménagement |
| onsite-2 | 192.168.1.51 | node2 | 10.99.0.3 | Izeron | idem + passerelle Izeron — **bientôt obsolète**, gardé jusqu'au déménagement |
| OVH | 51.210.105.102 | node3 | 10.99.0.1 | OVH | voteur etcd + relais WireGuard |
| alarm-1 | 172.16.1.118 | node4 | 10.99.0.4 | Beaufort | non votant + passerelle Beaufort ; **reste à Beaufort** |
| alarm-2 | 172.16.1.119 | node5 | 10.99.0.5 | Beaufort | non votant ; **reste à Beaufort** |
| alarm-3 | 172.16.1.120 | node6 | 10.99.0.6 | Izeron | **futur nœud d'Izeron**, entre d'abord comme non votant |
| alarm-4 | 172.16.1.121 | node7 | 10.99.0.7 | Izeron | **futur nœud d'Izeron**, entre d'abord comme non votant |

- alarm-3 et alarm-4 rejoignent le cluster comme **non votants** node6 / node7 ([dernière section](#ajouter-un-nœud-non-votant-patroni--backend-sans-etcd)). Ils sont affichés « Izeron » dès maintenant, alors qu'ils sont **physiquement à Beaufort jusqu'au déménagement** : le panneau Résilience et la panne contrôlée (`site:izeron`) les comptent à Izeron (limite assumée, INV-161).
- onsite-1 / onsite-2 restent voteurs etcd et en service jusqu'au déménagement ; l'interface les marque « bientôt obsolète » (`retiring_nodes`, INV-161). Le moment et la manière dont alarm-3/alarm-4 prendront leur place de voteur ne sont pas encore décidés : les §0-§5 ci-dessous décrivent le remplacement d'un voteur à `NODE_NAME` et IP WireGuard constants.
- Affichage (web, onglets Cluster et Tests) : `machine · nœud` (« alarm-3 · node6 ») d'après les clés SystemConfig `node_machines` et `retiring_nodes` (INV-161) ; un changement de machine ou de site se fait par ces clés, sans redéploiement.

> **2026-10-27** : alarm-1 et alarm-2 sont entrés en prod comme nœuds **non votants** node4 / node5.

Pour remplacer un voteur, on garde le **même `NODE_NAME` et la même IP WireGuard** que le nœud remplacé. Ainsi `.env.prod.nodeN`, `ETCD_INITIAL_CLUSTER`, l'orchestrateur CD (`alarm-cd-orchestrate`, `NODES_ORDER=(3 7 6 5 4 2 1)`) et les URLs de l'app ne changent pas. Seuls changent la clé WireGuard, les clés SSH et l'IP LAN (et `node_machines`, pour l'affichage).

## 0. Avant le jour J (nouvelle machine prête, hors cluster)
- Provisionnement de base fait : `docs/PROVISIONING_ONSITE.md` §2-§12, Docker §14, `/opt/alarm` cloné, `infra/onsite/install.sh` lancé.
- Passerelle : posée et démarrée par `install.sh` sur **chaque** machine (INV-182) ; poser `GATEWAY_KEY` et `GATEWAY_SITE` dans `/etc/alarm-gateway.env` (`docs/PROVISIONING_ONSITE.md` §21). Si la machine porte le modem : modem testé (SMS + appel).
- Timers CD installés : `alarm-cd-pull` (`infra/onsite/systemd/alarm-cd-pull.*`, **non installé par `install.sh`**), `alarm-host-sync`, `alarm-gateway-sync`.
- Images `:stable` déjà en cache (`alarm-cd-pull` a tourné une fois).
- Clé WireGuard prod générée (`wg genkey`), mais `wg0` prod **pas encore monté** : l'IP 10.99.0.X est encore portée par l'ancien nœud.
- Secrets copiés depuis l'ancien nœud, mode 600, propriétaire `alarm` :
  - `/opt/alarm/.env.prod.secrets` (même `SECRET_KEY` sur les 3 nœuds) ;
  - `/etc/alarm-gateway.env` : `GATEWAY_KEY` (copié) et `GATEWAY_SITE` (site de la **nouvelle** machine), sur chaque machine.
- Clé SSH de l'orchestrateur NODE3 ajoutée dans `~alarm/.ssh/authorized_keys`, avec la règle ufw `allow from 10.99.0.0/24 to any port 22 proto tcp` (`docs/CD_DESIGN.md` §9bis).
- Vérifier sur le web prod que les 3 nœuds sont sains et qu'**aucune alarme n'est active**.

## 1. Retirer l'ancien nœud
Sur l'ancien nœud :
```bash
cd /opt/alarm && docker compose --env-file .env.prod.nodeN -p nodeN down   # garder les volumes : retour arrière possible
sudo systemctl disable --now wg-quick@wg0 alarm-sms-gateway 2>/dev/null   # libere l'IP WG (+ le modem si node2)
```
Depuis un nœud restant (ici NODE3, etcd sur 10.99.0.1) :
```bash
E="docker exec node3-etcd-1 etcdctl --endpoints=http://10.99.0.1:2379"
$E member list -w table                          # noter l'ID de nodeN
$E member remove <ID_nodeN>
$E member add nodeN --peer-urls=http://10.99.0.X:2380
```
etcd tourne alors à 2 membres actifs sur 3 déclarés : le quorum (2) est tenu. Patroni garde son leader ou en élit un sur les 2 nœuds restants.

## 2. Mettre la nouvelle machine dans le mesh
1. Monter `wg0` prod avec l'IP 10.99.0.X et la **nouvelle** clé (`infra/onsite/wg0.conf.example`).
2. Sur NODE3 et sur l'autre nœud sur site, remplacer la clé publique de l'ancien peer par la nouvelle (`infra/onsite/peers.md` §Procédure d'ajout d'un peer), puis `wg syncconf`.
3. Vérifier les poignées de main : `sudo wg show` (latest handshake < 2 min), puis `ping 10.99.0.1`.
4. Mettre à jour la table des clés publiques dans `infra/onsite/peers.md`.

## 3. Démarrer le nouveau nœud
```bash
cd /opt/alarm
ETCD_INITIAL_CLUSTER_STATE=existing ./scripts/start-prod-node.sh --pull N
```
- etcd rejoint le cluster (`existing`). La variable ne sert qu'au premier démarrage : une fois `/etcd-data` initialisé, la valeur par défaut `new` est sans effet.
- Patroni trouve un leader dans etcd et, son volume étant vide, **se clone en replica** depuis lui (basebackup). Rien à faire.
- Passerelle et pile modem : déjà actives (`install.sh`, INV-182). Brancher le modem et l'Arduino suffit (section suivante).

## 4. Vérifier
```bash
$E member list -w table && $E endpoint health --cluster              # 3 membres sains
docker exec nodeN-patroni-1 patronictl list                           # nodeN « replica », lag ~0
curl -s http://10.99.0.X:8000/health                                  # role replica, boucle vivante
```
- Web prod, onglet Cluster : 3 nœuds au vert.
- Nœud passerelle : `Gateway operationnelle`, modem inscrit (icône signal), puis une **alarme de test** complète (SMS → appel → escalade → résolution).
- CD : au prochain tag promu, `alarm-cd-orchestrate` doit passer sur les 3 nœuds (SSH depuis NODE3 OK).

## 5. Retour arrière (nouveau nœud en échec)
1. Nouveau nœud : `docker compose … down`, puis `wg-quick down wg0`.
2. `$E member remove <ID_nouveau>` puis `$E member add nodeN --peer-urls=http://10.99.0.X:2380`.
3. Ancien nœud : remettre son ancienne clé WireGuard comme peer sur les 2 autres nœuds, puis `wg-quick up wg0`.
4. **Vider son volume etcd** : son ancien ID de membre a été retiré. Démarrer avec `ETCD_INITIAL_CLUSTER_STATE=existing ./scripts/start-prod-node.sh --pull N`. Son volume Patroni est conservé et rattrape le leader.

## Ordre conseillé
1. **node1 (onsite-1) en premier** : pas de modem, risque le plus faible.
2. Après 24 h stables, **node2 (onsite-2)**, qui déplace aussi la passerelle SMS/appels.

## Le matériel (modem, Arduino) peut changer de machine
Depuis le 2026-11-05 (INV-182, INV-183, INV-184), chaque machine sur site a la passerelle et la pile modem, actives même sans matériel : le modem SIM7600 et l'Arduino du contact sec se débranchent d'une machine et se rebranchent sur une autre **sans aucune commande**. La passerelle de la machine d'arrivée trouve le modem en 15 s au plus (puis son démarrage) et l'Arduino en 5 s, et les rattache à **son** `GATEWAY_SITE` (le site suit la machine, pas le matériel : vérifier `GATEWAY_KEY` et `GATEWAY_SITE` de la machine d'arrivée). **Un seul modem par site à la fois** : deux modems sur un même site enverraient chaque SMS et chaque appel deux fois. Effets attendus sur la machine de départ, une fois chacun : le watchdog tente **une** récupération USB et envoie **un** email « Modem SIM7600 DOWN » (normal, il attend ensuite sans rien toucher ; un reboot ou `sudo rm /run/alarm-modem-seen` lui fait oublier ce modem) ; si l'Arduino est parti, l'email « Passerelle <machine> muette » au bout de 10 min (INV-172 ; la ligne est oubliée après 7 jours). L'indicateur de signal de l'ancienne machine reste « inconnu » dans le web et l'app (INV-158) tant que sa ligne `modem_status:<machine>` existe : la supprimer de `system_config` sur le leader si elle gêne. Un modem rebranché sur le même site en moins de 10 min ne déclenche pas l'email « Site … sans modem » (INV-185) ; au-delà, un email part, et le suivant seulement après un retour puis une nouvelle absence.

---

## Ajouter un nœud non votant (Patroni + backend, sans etcd)

Décisions du propriétaire (node4/node5 le 2026-10-27, node6/node7 le 2026-11-05) :

| Machine | `NODE_NAME` | IP WireGuard | Site affiché | Rôle |
|---|---|---|---|---|
| alarm-1 | node4 | 10.99.0.4 | Beaufort | Patroni + backend + passerelle du site **beaufort** |
| alarm-2 | node5 | 10.99.0.5 | Beaufort | Patroni + backend |
| alarm-3 | node6 | 10.99.0.6 | Izeron (physiquement à Beaufort jusqu'au déménagement) | Patroni + backend |
| alarm-4 | node7 | 10.99.0.7 | Izeron (physiquement à Beaufort jusqu'au déménagement) | Patroni + backend |

Un nœud non votant n'a **aucun conteneur etcd** : son Patroni utilise les 3 etcd votants (10.99.0.1/.2/.3) via WireGuard, relayés par NODE3 (`infra/onsite/peers.md`). Le quorum etcd reste 2 sur 3 : perdre le site beaufort ne le touche pas, et `ETCD_INITIAL_CLUSTER` ne change pas. `.env.prod.node4` à `.env.prod.node7` existent dans le repo ; les `PEER_TEST_URLS` des 7 nœuds listent déjà 10.99.0.6 et 10.99.0.7.

**Avant l'arrivée de node6/node7** (le dépôt les annonce déjà) : chaque passage du CD les trouve injoignables et les reporte (`partial`, sans cooldown, les 5 autres sont déployés, INV-167) ; la panne contrôlée refuse toute demande (« membre absent du cluster Patroni : node6 », INV-176) — pour tester avant leur arrivée, enregistrer `node_locations` sans eux (SystemConfig).

1. **Avant** : comme le §0 ci-dessus (provisionnement, timers CD, `.env.prod.secrets`, clé SSH de l'orchestrateur). WireGuard : générer la clé de la machine, déclarer son peer `10.99.0.X/32` **sur NODE3 seulement** (relais, `infra/onsite/peers.md`), noter la clé publique dans `peers.md`. `wg0` monté avec 10.99.0.X, puis vérifier que les votants répondent à travers le relais : `curl -s http://10.99.0.1:2379/health` (idem `.2`, `.3`).
2. **Capacité de réplication, une seule fois avant le 1er ajout** : 4 replicas + un clonage dépassent `max_wal_senders=5`. `patroni/patroni.yml` (10) ne sert qu'au bootstrap. À 7 nœuds (6 replicas + un clonage = 7), 10 suffit encore : avant node6/node7, vérifier seulement `$P show-config` (`max_wal_senders: 10`, `max_replication_slots: 10`). Sur le cluster en service, sans alarme active :
   ```bash
   P="docker exec node3-patroni-1 patronictl -c /etc/patroni/patroni.yml"
   $P edit-config --pg max_wal_senders=10 --pg max_replication_slots=10 --force
   $P list                                # « Pending restart » sur chaque membre
   $P restart alarm-cluster <replica> --force   # chaque replica d'abord, le leader en dernier
   ```
3. **Démarrer** : `cd /opt/alarm && ./scripts/start-prod-node.sh --pull N` (N = 4 à 7). Le script ne lance que `patroni` et `backend` (`--no-deps`). Ni `ETCD_INITIAL_CLUSTER_STATE`, ni `etcdctl member add` : le nœud n'est pas membre etcd. Patroni se clone en replica depuis le leader.
4. **Vérifier** :
   ```bash
   docker compose --env-file .env.prod.nodeN -p nodeN ps   # patroni + backend, PAS d'etcd
   $E member list -w table                                  # toujours 3 membres etcd
   $P list                                                  # nodeN « replica », lag ~0
   curl -s http://10.99.0.X:8000/health                     # role replica
   ```
5. **Passerelle (alarm-1)** : comme un nœud passerelle du §0, avec `GATEWAY_SITE=beaufort` dans `/etc/alarm-gateway.env`. `install.sh` y ajoute `NODE4_URL` à `NODE7_URL` s'ils manquent (sur toutes les passerelles, au prochain `alarm-host-sync`).
6. **CD** : l'orchestrateur suit `NODES_ORDER=(3 7 6 5 4 2 1)`. Depuis le 2026-10-29 (INV-167), un non votant (node4 à node7) qui ne répond pas est **reporté au prochain passage** sans bloquer les autres nœuds ; il reçoit la version courante dès qu'il revient (`docs/CD_DESIGN.md` §4, « Déploiement indépendant par site »). Les scripts CD et le runner de panne contrôlée de NODE3 sont **installés à la main** (`docs/CD_DESIGN.md` §9bis, §9ter) : les réinstaller depuis `/opt/alarm` après le merge qui ajoute un nœud.
7. **Affichage** : machine et site viennent de `node_machines` / `node_locations` (SystemConfig, INV-161). Défauts déjà prévus pour node4 à node7 ; une clé enregistrée en base remplace son défaut entier : la compléter si elle existe.

**Retrait** : `docker compose --env-file .env.prod.nodeN -p nodeN down` sur le nœud. Aucun geste etcd. Patroni oublie le membre au bout du TTL.

---

## Ordre des nœuds dans `/etc/alarm-gateway.env` (passerelle, INV-174)

La passerelle cherche le backend primary dans l'ordre `NODE1_URL … NODE7_URL` et **mémorise** le dernier trouvé : en régime établi, une requête ne coûte qu'un `/health` ; après une bascule, l'ancien primary coûte `HTTP_TIMEOUT_SECONDS` (10 s, sa sonde garde la tolérance des échanges) puis le balayage repart dans l'ordre configuré, à 3 s par autre nœud mort (constante du code, INV-174). Tant qu'aucun nœud ne répond `primary` (élection, cluster sans quorum), chaque requête refait le balayage complet à 3 s par nœud. `HTTP_TIMEOUT_SECONDS` (10 s) reste celui des échanges de données et de la sonde du primary mémorisé : ne pas le baisser.

Ordre conseillé (son site d'abord, OVH ensuite, l'autre site en dernier) — **édition manuelle optionnelle**, `install.sh` ne la fait pas :

| Passerelle | `NODE1_URL` | `NODE2_URL` | `NODE3_URL` | `NODE4_URL` | `NODE5_URL` | `NODE6_URL` | `NODE7_URL` |
|---|---|---|---|---|---|---|---|
| Izeron (onsite-2) | `http://10.99.0.3:8000` | `http://10.99.0.2:8000` | `http://10.99.0.1:8000` | `http://10.99.0.4:8000` | `http://10.99.0.5:8000` | `http://10.99.0.6:8000` | `http://10.99.0.7:8000` |
| Beaufort (alarm-1) | `http://10.99.0.4:8000` | `http://10.99.0.5:8000` | `http://10.99.0.1:8000` | `http://10.99.0.2:8000` | `http://10.99.0.3:8000` | `http://10.99.0.6:8000` | `http://10.99.0.7:8000` |

Les noms `NODEx_URL` ne désignent pas un nœud : seule la position compte. Après édition : `sudo systemctl restart alarm-sms-gateway`.
