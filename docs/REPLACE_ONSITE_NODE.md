# Remplacer un nœud sur site du cluster de prod

Remplacer onsite-1 ou onsite-2 par une nouvelle machine **sans interrompre le service**.
On remplace **un nœud à la fois** : etcd et Patroni gardent 2 membres sur 3 et les alarmes continuent.

Cible prévue (choix du propriétaire, 2026-10-22) :

| Nouvelle machine | Remplace | `NODE_NAME` | IP WireGuard prod | Rôle |
|---|---|---|---|---|
| alarm-2 (172.16.1.121) | onsite-1 | node1 | 10.99.0.2 | backend + etcd + Patroni |
| alarm-1 (172.16.1.120) | onsite-2 | node2 | 10.99.0.3 | idem + passerelle SMS/appels (nouveau SIM7600) |

On garde le **même `NODE_NAME` et la même IP WireGuard** que le nœud remplacé. Ainsi `.env.prod.nodeN`, `ETCD_INITIAL_CLUSTER`, l'orchestrateur CD (`alarm-cd-orchestrate`, `NODES_ORDER=(3 2 1)`) et les URLs de l'app ne changent pas. Seuls changent la clé WireGuard, les clés SSH et l'IP LAN.

## 0. Avant le jour J (nouvelle machine prête, hors cluster)
- Provisionnement de base fait : `docs/PROVISIONING_ONSITE.md` §2-§12, Docker §14, `/opt/alarm` cloné, `infra/onsite/install.sh` lancé.
- Pour le nœud passerelle : utilisateur `alarm-gateway` créé, unité `alarm-sms-gateway`, udev, voix, modem testé (SMS + appel).
- Timers CD installés : `alarm-cd-pull` (`infra/onsite/systemd/alarm-cd-pull.*`, **non installé par `install.sh`**), `alarm-host-sync`, `alarm-gateway-sync`.
- Images `:stable` déjà en cache (`alarm-cd-pull` a tourné une fois).
- Clé WireGuard prod générée (`wg genkey`), mais `wg0` prod **pas encore monté** : l'IP 10.99.0.X est encore portée par l'ancien nœud.
- Secrets copiés depuis l'ancien nœud, mode 600, propriétaire `alarm` :
  - `/opt/alarm/.env.prod.secrets` (même `SECRET_KEY` sur les 3 nœuds) ;
  - `/etc/alarm-gateway.env` (`GATEWAY_KEY`), sur le nœud passerelle seulement.
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
- Nœud passerelle : `sudo systemctl enable --now alarm-sms-gateway alarm-modem-rat alarm-modem-watchdog alarm-4g-standby`.

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
1. **node1 (onsite-1 → alarm-2) en premier** : pas de modem, risque le plus faible.
2. Après 24 h stables, **node2 (onsite-2 → alarm-1)**, qui déplace aussi la passerelle SMS/appels.
