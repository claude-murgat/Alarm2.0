# Remplacer un nœud sur site du cluster de prod

Remplacer onsite-1 ou onsite-2 par une nouvelle machine **sans interrompre le service**.
On remplace **un nœud à la fois** : etcd et Patroni gardent 2 membres sur 3 et les alarmes continuent.

Cible (décision du propriétaire du 2026-11-05 ; remplace le plan du 2026-10-22 où alarm-1/alarm-2 devaient remplacer onsite-1/onsite-2) :

| Machine | IP LAN | `NODE_NAME` | IP WireGuard | Site affiché (`node_locations`) | Rôle |
|---|---|---|---|---|---|
| onsite-1 | 192.168.1.50 | node1 | 10.99.0.2 | Izeron | **retiré le jour du déménagement** (plus membre etcd, plus déployé) |
| onsite-2 | 192.168.1.51 | node2 | 10.99.0.3 | Izeron | **retiré le jour du déménagement** ; sa passerelle passe sur alarm-3 |
| OVH | 51.210.105.102 | node3 | 10.99.0.1 | OVH | voteur etcd + relais WireGuard |
| alarm-1 | 172.16.1.118 | node4 | 10.99.0.4 | Beaufort | non votant + passerelle Beaufort ; **reste à Beaufort** |
| alarm-2 | 172.16.1.119 | node5 | 10.99.0.5 | Beaufort | non votant ; **reste à Beaufort** |
| alarm-3 | (LAN d'Izeron) | node6 | 10.99.0.6 | Izeron | **voteur etcd** + Patroni + backend + passerelle Izeron (jour du déménagement) |
| alarm-4 | (LAN d'Izeron) | node7 | 10.99.0.7 | Izeron | **voteur etcd** + Patroni + backend (jour du déménagement) |

- alarm-3 et alarm-4 rejoignent le cluster comme **non votants** node6 / node7 ([dernière section](#ajouter-un-nœud-non-votant-patroni--backend-sans-etcd)). Ils sont affichés « Izeron » dès maintenant, alors qu'ils sont **physiquement à Beaufort jusqu'au déménagement** : le panneau Résilience et la panne contrôlée (`site:izeron`) les comptent à Izeron (limite assumée, INV-161).
- onsite-1 / onsite-2 restent voteurs etcd et en service jusqu'au déménagement ; l'interface les marque « bientôt obsolète » (`retiring_nodes`, INV-161). **Décision du propriétaire du 2026-11-05 (INV-090)** : les voteurs ne changent que **le jour du déménagement** : node6 / node7 deviennent voteurs (`etcdctl member add`, un à la fois), node1 / node2 sont retirés d'etcd ; état final 3 voteurs node3, node6, node7. Runbook « Jour du déménagement » et configuration finale : PR brouillon dédiée, à ne fusionner que ce jour-là. **Pas avant** : tant qu'alarm-3/4 sont à Beaufort, tout passe par le relais NODE3, et 5 voteurs (dont 2 à Beaufort) perdraient le leader si OVH tombe. Le panneau Résilience montre déjà ce que le déménagement corrige : ligne « Legacy débranché (node1 + node2) », aujourd'hui sans leader (INV-175). Les §0-§5 ci-dessous décrivent le remplacement d'un voteur à `NODE_NAME` et IP WireGuard constants.
- Affichage (web, onglets Cluster et Tests ; app Android) : `machine · nœud` (« alarm-3 · node6 ») d'après les clés SystemConfig `node_machines` et `retiring_nodes`, groupé « Izeron », « Beaufort », « OVH », puis « Legacy (Izeron) » en dernier (champ `group`, INV-161) ; un changement de machine ou de site se fait par ces clés, sans redéploiement.

> **2026-10-27** : alarm-1 et alarm-2 sont entrés en prod comme nœuds **non votants** node4 / node5.

Pour remplacer un voteur, on garde le **même `NODE_NAME` et la même IP WireGuard** que le nœud remplacé. Ainsi `.env.prod.nodeN`, `ETCD_INITIAL_CLUSTER`, l'orchestrateur CD (`alarm-cd-orchestrate`, `NODES_ORDER=(3 5 4 7 6)`, `VOTERS=(3 6 7)`) et les URLs de l'app ne changent pas. Seuls changent la clé WireGuard, les clés SSH et l'IP LAN (et `node_machines`, pour l'affichage).

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

Un nœud non votant n'a **aucun conteneur etcd** : son Patroni utilise les 3 etcd voteurs (10.99.0.1/.6/.7 depuis le jour du déménagement ; .1/.2/.3 avant) via WireGuard, relayés par NODE3 (`infra/onsite/peers.md`). Le quorum etcd reste 2 sur 3 : perdre le site beaufort ne le touche pas, et `ETCD_INITIAL_CLUSTER` ne change pas. `.env.prod.node4` à `.env.prod.node7` existent dans le repo ; les `PEER_TEST_URLS` des 7 nœuds listent déjà 10.99.0.6 et 10.99.0.7.

**Avant l'arrivée de node6/node7** (le dépôt les annonce déjà) : chaque passage du CD les trouve injoignables et les reporte (`partial`, sans cooldown, les 5 autres sont déployés, INV-167) ; la panne contrôlée refuse toute demande (« membre absent du cluster Patroni : node6 », INV-176) — pour tester avant leur arrivée, enregistrer `node_locations` sans eux (SystemConfig).

1. **Avant** : comme le §0 ci-dessus (provisionnement, timers CD, `.env.prod.secrets`, clé SSH de l'orchestrateur). WireGuard : générer la clé de la machine, déclarer son peer `10.99.0.X/32` **sur NODE3 seulement** (relais, `infra/onsite/peers.md`), noter la clé publique dans `peers.md`. `wg0` monté avec 10.99.0.X, puis vérifier que les votants répondent à travers le relais : `curl -s http://10.99.0.1:2379/health` (idem `.2`, `.3`).
2. **Capacité de réplication, une seule fois avant le 1er ajout** : 4 replicas + un clonage dépassent `max_wal_senders=5`. `patroni/patroni.yml` (10) ne sert qu'au bootstrap. À 7 nœuds (6 replicas + un clonage = 7), 10 suffit encore : avant node6/node7, vérifier seulement `$P show-config` (`max_wal_senders: 10`, `max_replication_slots: 10`). Sur le cluster en service, sans alarme active :
   ```bash
   P="docker exec node3-patroni-1 patronictl -c /etc/patroni/patroni.yml"
   $P edit-config --pg max_wal_senders=10 --pg max_replication_slots=10 --force
   $P list                                # « Pending restart » sur chaque membre
   $P restart alarm-cluster <replica> --force   # chaque replica d'abord, le leader en dernier
   ```
3. **Démarrer** : `cd /opt/alarm && ./scripts/start-prod-node.sh --pull N` (N = 4 ou 5 ; 6 et 7 sont voteurs depuis le jour du déménagement). Le script ne lance que `patroni` et `backend` (`--no-deps`). Ni `ETCD_INITIAL_CLUSTER_STATE`, ni `etcdctl member add` : le nœud n'est pas membre etcd. Patroni se clone en replica depuis le leader.
4. **Vérifier** :
   ```bash
   docker compose --env-file .env.prod.nodeN -p nodeN ps   # patroni + backend, PAS d'etcd
   $E member list -w table                                  # toujours 3 membres etcd
   $P list                                                  # nodeN « replica », lag ~0
   curl -s http://10.99.0.X:8000/health                     # role replica
   ```
5. **Passerelle (alarm-1)** : comme un nœud passerelle du §0, avec `GATEWAY_SITE=beaufort` dans `/etc/alarm-gateway.env`. `install.sh` y ajoute `NODE4_URL` à `NODE7_URL` s'ils manquent (sur toutes les passerelles, au prochain `alarm-host-sync`).
6. **CD** : l'orchestrateur suit `NODES_ORDER=(3 5 4 7 6)`. Depuis le 2026-10-29 (INV-167), un non votant (node4 à node7) qui ne répond pas est **reporté au prochain passage** sans bloquer les autres nœuds ; il reçoit la version courante dès qu'il revient (`docs/CD_DESIGN.md` §4, « Déploiement indépendant par site »). Les scripts CD et le runner de panne contrôlée de NODE3 sont **installés à la main** (`docs/CD_DESIGN.md` §9bis, §9ter) : les réinstaller depuis `/opt/alarm` après le merge qui ajoute un nœud.
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

---

## Jour du déménagement : onsite-1/2 retirés, alarm-3/4 voteurs à Izeron

**État final** (décision du propriétaire du 2026-11-05, INV-090) : **3 voteurs etcd node3 (OVH), node6 (alarm-3), node7 (alarm-4)**, quorum 2 ; node4/node5 (Beaufort) non votants ; onsite-1/onsite-2 (node1/node2) retirés d'etcd, de Patroni et du CD. La PR qui porte cet état (« NE PAS FUSIONNER AVANT LE JOUR J ») n'est fusionnée qu'à l'étape 6.

Notation (sur NODE3) :
```bash
E="docker exec node3-etcd-1 etcdctl --endpoints=http://10.99.0.1:2379"
P="docker exec node3-patroni-1 patronictl -c /etc/patroni/patroni.yml"
```
Marge = nombre de voteurs etcd qu'on peut encore perdre sans perdre le quorum. **Marge 0 = enchaîner l'étape suivante sans attendre**, aucune autre intervention en parallèle.

**Prérequis** : aucune alarme active ; `$E endpoint health --cluster` → node1, node2, node3 sains ; `$P list` → node6/node7 « replica » `streaming` (non votants, installés) ; la PR des groupes d'affichage (#298) fusionnée et déployée.

**0. Mettre l'orchestrateur en pause** (NODE3) : `sudo systemctl stop alarm-cd-orchestrate.timer` (et `alarm-chaos.timer` s'il est actif). Aucun déploiement ne doit relancer etcd ou Patroni pendant la bascule.
Marge : 3 voteurs (node1, node2, node3), quorum 2 → **marge 1**.

**1. Brancher alarm-3/4 à Izeron (toujours non votants) et déplacer la passerelle d'Izeron.**
- Arrêter alarm-3 et alarm-4 à Beaufort, les brancher au LAN d'Izeron. Leurs IP WireGuard (10.99.0.6/.7) ne changent pas ; le tunnel se reconnecte seul (endpoint appris par `PersistentKeepalive`). Vérifier : `$P list` → node6/node7 `streaming`, lag ~0.
- Passerelle : `GATEWAY_SITE=izeron` dans `/etc/alarm-gateway.env` d'alarm-3 et d'alarm-4 (puis `sudo systemctl restart alarm-sms-gateway`) ; débrancher le modem et l'Arduino d'onsite-2, les brancher sur alarm-3 (INV-182 : aucune commande, **un seul modem par site**). Panneau Résilience : SystemConfig `gateway_hosts` = `alarm-3:node6,alarm-1:node4` (défaut après l'étape 6 ; passerelle sur alarm-4 → `alarm-4:node7`). Faire une **alarme de test** à Izeron (SMS + appel).
- Les alarmes et SMS d'Izeron sont coupés le temps du déplacement du modem (quelques minutes).
Marge etcd inchangée : **marge 1**.

**2. node6 devient voteur.**
- Sur NODE3 : `$E member add node6 --peer-urls=http://10.99.0.6:2380` → imprime `ETCD_INITIAL_CLUSTER="node1=…,node2=…,node3=…,node6=…"` (4 membres) : le copier.
  4 membres déclarés, 3 en marche, quorum 3 → **marge 0** jusqu'au démarrage de node6.
- Sur alarm-3 (le dépôt y est encore à l'état d'avant cette PR : `start-prod-node.sh 6` n'y lance pas etcd, d'où la commande directe) :
  ```bash
  cd /opt/alarm && set -a && . ./.env.prod.secrets && set +a
  docker volume ls | grep node6_etcd_data    # rien attendu (sinon : docker volume rm node6_etcd_data)
  ETCD_INITIAL_CLUSTER='<valeur imprimée par member add>' ETCD_INITIAL_CLUSTER_STATE=existing \
    docker compose --env-file .env.prod.node6 -p node6 up -d etcd
  ```
  `ETCD_INITIAL_CLUSTER` doit lister **exactement** les membres déclarés (sinon etcd refuse : « member count is unequal ») ; la variable du shell l'emporte sur `--env-file` ; les deux ne servent qu'au premier démarrage.
- Attendre `$E endpoint health --cluster` : 4 membres sains.
  4 en marche, quorum 3 → **marge 1**.

**3. node7 devient voteur.** Même chose : `$E member add node7 --peer-urls=http://10.99.0.7:2380` (5 membres, 4 en marche, quorum 3 → **marge 1**), puis sur alarm-4 la même commande avec `-p node7`, `.env.prod.node7` et la liste à 5 membres imprimée, `node7_etcd_data`. Attendre 5 membres sains.
5 en marche, quorum 3 → **marge 2**.

**4. Débrancher onsite-1 et onsite-2.**
- Si le leader Patroni (`$P list`) est node1 ou node2 : `$P switchover alarm-cluster --leader <node1|node2> --candidate node6 --force`, attendre node6 leader.
- Sur onsite-1 puis onsite-2 : `docker compose --env-file .env.prod.nodeN -p nodeN down` et `sudo systemctl disable --now alarm-sms-gateway`, puis arrêt de la machine.
5 membres déclarés, 3 en marche (node3, node6, node7), quorum 3 → **marge 0** : étape 5 aussitôt.

**5. Retirer node1 et node2 d'etcd** (NODE3) :
```bash
$E member list -w table                  # noter les ID de node1 et node2
$E member remove <ID_node1>              # 4 membres, 3 en marche, quorum 3 : marge 0
$E member remove <ID_node2>              # 3 membres, 3 en marche, quorum 2 : marge 1
$E endpoint health --cluster             # node3, node6, node7 sains
```
Patroni oublie node1/node2 au bout du TTL. Jusqu'à l'étape 6, les Patroni ont encore `ETCD_HOSTS` = 10.99.0.1/.2/.3 : ils joignent etcd par 10.99.0.1 et la liste des membres qu'etcd leur donne (découverte de Patroni, `use_proxies` désactivé) ; vérifier `$P list` (leader présent).
3 voteurs, quorum 2 → **marge 1**.

**6. Fusionner la PR « NE PAS FUSIONNER AVANT LE JOUR J », réinstaller, reprendre le CD.**
- Fusionner, publier et promouvoir `:stable` comme d'habitude (`docs/CD_DESIGN.md`) : l'image porte les nouveaux défauts (`etcd_voters`, lieux, machines, `retiring_nodes` vide, `gateway_hosts`, email de fin de chaîne sur node6).
- Attendre que `/opt/alarm` soit à jour sur chaque nœud (`alarm-host-sync`) : `.env.prod.node3..7` listent les 3 voteurs, `.env.prod.node1/2` disparaissent.
- NODE3 : réinstaller les scripts posés à la main (`docs/CD_DESIGN.md` §9bis, §9ter : orchestrateur, canary, rollback, `alarm-chaos.sh`) ; forcer le redéploiement des 5 nœuds (`sudo rm /var/lib/alarm/cd/node*.digest`) ; `sudo systemctl start alarm-cd-orchestrate.timer`.
- Le CD redéploie 3, 5, 4, 7, 6 : chaque Patroni reçoit `ETCD_HOSTS` = 10.99.0.1/.6/.7 ; les etcd de node3/node6/node7 sont recréés (le nouveau `ETCD_INITIAL_CLUSTER` est ignoré : données existantes). Pendant le redémarrage d'un voteur : 2 sur 3, **marge 0** (comme tout déploiement à 3 voteurs) ; sinon **marge 1**.
- SystemConfig : si `node_locations`, `node_machines`, `retiring_nodes`, `etcd_voters` ou `gateway_hosts` ont été enregistrées à la main, les corriger ou les supprimer (une clé enregistrée remplace son défaut ; une valeur vide vaut défaut).

**7. Vérifier.**
- `$E member list -w table` : node3, node6, node7 seuls ; `$E endpoint health --cluster` : 3 sains.
- `$P list` : node3 à node7, un leader, replicas `streaming`, lag ~0.
- Web, onglet Cluster : groupes Izeron (alarm-3, alarm-4), Beaufort (alarm-1, alarm-2), OVH ; plus de « Legacy (Izeron) ». Onglet Tests, Résilience : voteurs node3, node6, node7 ; « OVH » → leader oui (2/3, vrai physiquement désormais) ; plus de ligne « Legacy débranché ».
- Alarme de test à Izeron et à Beaufort (SMS, appel, escalade, résolution).
- CD : passage suivant `success` sur les 5 nœuds ; réactiver `alarm-chaos.timer` s'il l'était.
- Ensuite : passerelles (`/etc/alarm-gateway.env`) sans `NODEx_URL` vers 10.99.0.2/.3 (sinon 3 s perdues par nœud mort à chaque bascule, INV-174) ; URLs de l'app Android ; `infra/onsite/peers.md` (peers 10.99.0.2/.3 à retirer de NODE3).

**Retour arrière** : avant l'étape 4, `$E member remove` de node7 puis node6 et `docker compose … rm -sf etcd && docker volume rm nodeN_etcd_data` sur alarm-3/4 (on retrouve node1, node2, node3). Après l'étape 5, revenir en arrière = ré-ajouter onsite-1/2 comme de nouveaux voteurs (même procédure, volumes etcd vidés).
