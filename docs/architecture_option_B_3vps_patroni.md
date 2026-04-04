# Option B — 3 noeuds Patroni + etcd (primary-replica avec quorum)

## Contexte

- 10 utilisateurs, 5 alarmes/nuit, 1 seule alarme active a la fois
- Objectif : survie complete si un noeud ou un site tombe
- Contrainte : meme code source, meme docker-compose, seules les variables d'env changent

## Topologie physique

```
Baie serveur (site client)          Datacenter A (cloud)           Datacenter B (cloud)
┌────────────────────┐             ┌────────────────────┐         ┌────────────────────┐
│       NODE1        │             │       NODE2        │         │       NODE3        │
│                    │             │                    │         │                    │
│  Backend (FastAPI) │             │  Backend (FastAPI) │         │  Backend (FastAPI) │
│  Patroni           │             │  Patroni           │         │  Patroni           │
│  PostgreSQL        │             │  PostgreSQL        │         │  PostgreSQL        │
│  etcd              │             │  etcd              │         │  etcd              │
│                    │             │                    │         │                    │
│  (serveur physique │             │  (VPS cloud        │         │  (VPS cloud        │
│   dans la baie)    │             │   ~15 EUR/mois)    │         │   ~15 EUR/mois)    │
└────────────────────┘             └────────────────────┘         └────────────────────┘
         │                                  │                              │
         └──────────────────────────────────┴──────────────────────────────┘
                              Reseau internet (VPN ou direct)
```

- NODE1 : serveur physique dans la baie du site. Avantage : latence minimale pour les
  utilisateurs sur site, pas de cout cloud, fonctionne meme si internet est coupe
  (les apps sur le reseau local continuent de fonctionner).
- NODE2 : VPS cloud datacenter A (ex: Scaleway Paris, OVH Gravelines).
- NODE3 : VPS cloud datacenter B (ex: Scaleway Amsterdam, Hetzner Falkenstein).

Les 3 datacenters sont distincts : aucune panne locale ne peut affecter 2 noeuds.

## Architecture logicielle

```
Chaque noeud (identique) :
┌─────────────────────────────────────────┐
│  docker-compose.yml                     │
│  ┌───────────┐ ┌────────┐ ┌──────────┐ │
│  │  backend   │ │ patroni│ │   etcd   │ │
│  │  (FastAPI) │ │        │ │          │ │
│  │  port 8000 │ │        │ │ port 2379│ │
│  └─────┬─────┘ └───┬────┘ └────┬─────┘ │
│        │           │            │       │
│        │     ┌─────┴──────┐    │       │
│        └────>│ PostgreSQL │<───┘       │
│              │ port 5432  │            │
│              └────────────┘            │
└─────────────────────────────────────────┘
```

- Patroni gere PostgreSQL : replication, promotion, health checks
- etcd stocke le consensus : qui est le primary
- Le backend se connecte a son PostgreSQL local
- Patroni decide si ce PostgreSQL est primary (read-write) ou replica (read-only)

## Code source unique

Un seul repo, un seul docker-compose.yml, un seul Dockerfile.
Chaque noeud ne differe que par son fichier .env :

### docker-compose.yml (identique partout)

```yaml
services:
  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: alarm
      POSTGRES_PASSWORD: ${DB_PASSWORD}
      POSTGRES_DB: alarm_db
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U alarm -d alarm_db"]
      interval: 5s
      timeout: 3s
      retries: 10

  patroni:
    image: patroni/patroni
    environment:
      PATRONI_NAME: ${NODE_NAME}
      PATRONI_SCOPE: alarm-cluster
      PATRONI_ETCD_HOSTS: ${ETCD_HOSTS}
      PATRONI_POSTGRESQL_DATA_DIR: /var/lib/postgresql/data
      PATRONI_POSTGRESQL_CONNECT_ADDRESS: ${NODE_IP}:5432
      PATRONI_RESTAPI_CONNECT_ADDRESS: ${NODE_IP}:8008
      PATRONI_REPLICATION_USERNAME: replicator
      PATRONI_REPLICATION_PASSWORD: ${REPL_PASSWORD}
    depends_on:
      db:
        condition: service_healthy

  etcd:
    image: quay.io/coreos/etcd:v3.5
    environment:
      ETCD_NAME: ${NODE_NAME}
      ETCD_DATA_DIR: /etcd-data
      ETCD_LISTEN_CLIENT_URLS: http://0.0.0.0:2379
      ETCD_LISTEN_PEER_URLS: http://0.0.0.0:2380
      ETCD_ADVERTISE_CLIENT_URLS: http://${NODE_IP}:2379
      ETCD_INITIAL_ADVERTISE_PEER_URLS: http://${NODE_IP}:2380
      ETCD_INITIAL_CLUSTER: ${ETCD_CLUSTER}
      ETCD_INITIAL_CLUSTER_STATE: new
    volumes:
      - etcd_data:/etcd-data

  backend:
    build: ./backend
    ports:
      - "8000:8000"
    environment:
      - DATABASE_URL=postgresql://alarm:${DB_PASSWORD}@db:5432/alarm_db
      - NODE_NAME=${NODE_NAME}
      - PEER_URLS=${PEER_URLS}
      - SECRET_KEY=${SECRET_KEY}
    depends_on:
      db:
        condition: service_healthy
    restart: unless-stopped
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"

volumes:
  pgdata:
  etcd_data:
```

### .env par noeud

```bash
# NODE1 (baie serveur site)
NODE_NAME=node1
NODE_IP=192.168.1.100          # IP locale ou IP publique si VPN
DB_PASSWORD=alarm_secret
REPL_PASSWORD=rep_secret
SECRET_KEY=alarm-secret-key
PEER_URLS=http://node2.example.com:8000,http://node3.example.com:8000
ETCD_HOSTS=192.168.1.100:2379,node2.example.com:2379,node3.example.com:2379
ETCD_CLUSTER=node1=http://192.168.1.100:2380,node2=http://node2.example.com:2380,node3=http://node3.example.com:2380
```

```bash
# NODE2 (datacenter A)
NODE_NAME=node2
NODE_IP=51.xx.xx.xx
DB_PASSWORD=alarm_secret
REPL_PASSWORD=rep_secret
SECRET_KEY=alarm-secret-key
PEER_URLS=http://192.168.1.100:8000,http://node3.example.com:8000
ETCD_HOSTS=192.168.1.100:2379,51.xx.xx.xx:2379,node3.example.com:2379
ETCD_CLUSTER=node1=http://192.168.1.100:2380,node2=http://51.xx.xx.xx:2380,node3=http://node3.example.com:2380
```

```bash
# NODE3 (datacenter B)
NODE_NAME=node3
NODE_IP=52.yy.yy.yy
DB_PASSWORD=alarm_secret
REPL_PASSWORD=rep_secret
SECRET_KEY=alarm-secret-key
PEER_URLS=http://192.168.1.100:8000,http://node2.example.com:8000
ETCD_HOSTS=192.168.1.100:2379,node2.example.com:2379,52.yy.yy.yy:2379
ETCD_CLUSTER=node1=http://192.168.1.100:2380,node2=http://node2.example.com:2380,node3=http://52.yy.yy.yy:2380
```

## Deploiement

Identique sur les 3 noeuds :

```bash
git pull
cp .env.nodeX .env
docker compose up -d
```

## Comportement en cas de panne

### Panne d'un noeud

| Noeud en panne | Quorum | Primary | Disponibilite |
|----------------|--------|---------|---------------|
| NODE1 (baie site) | NODE2+NODE3 (2/3) | Promu parmi les survivants | OK |
| NODE2 (DC A) | NODE1+NODE3 (2/3) | Inchange ou promu | OK |
| NODE3 (DC B) | NODE1+NODE2 (2/3) | Inchange ou promu | OK |

### Panne d'un site/datacenter entier

| Site en panne | Noeuds perdus | Quorum | Disponibilite |
|---------------|--------------|--------|---------------|
| Baie site | NODE1 | NODE2+NODE3 (2/3) | OK |
| Datacenter A | NODE2 | NODE1+NODE3 (2/3) | OK |
| Datacenter B | NODE3 | NODE1+NODE2 (2/3) | OK |

Chaque site est independant : aucune panne locale ne touche 2 noeuds.

### Coupure reseau (noeud isole)

| Isole | Ce qui se passe |
|-------|-----------------|
| NODE1 isole | Patroni perd le quorum sur NODE1 → DB passe en read-only. NODE2+NODE3 ont le quorum → promotion. Pas de split-brain. |
| NODE2 isole | Meme mecanisme. NODE1+NODE3 continuent. |
| NODE3 isole | NODE1+NODE2 continuent. |
| 2 noeuds isoles | 1 seul noeud = pas de quorum = panne totale (voulu : mieux qu'un split-brain) |

### Internet coupe sur le site client

NODE1 (baie) est isole. Les apps sur le reseau LOCAL du site continuent de
fonctionner via NODE1 (le backend repond, la DB locale a les donnees).
Mais NODE1 ne peut plus etre primary (pas de quorum) → il passe en read-only.

Les apps peuvent LIRE les alarmes mais pas en creer/acquitter.
Quand internet revient, NODE1 se resynchronise automatiquement.

Pour permettre l'ecriture en mode degrade (site isole) : le backend peut
basculer en mode "local-only" et se resynchroniser apres reconnexion.
C'est un choix de design (disponibilite vs coherence).

## Avantages du noeud sur site (NODE1 dans la baie)

1. Latence minimale pour les utilisateurs sur site (reseau local, pas internet)
2. Fonctionne meme si internet est coupe (mode lecture)
3. Pas de cout cloud pour ce noeud
4. Les donnees restent physiquement sur site (conformite, souverainete)
5. Monitoring direct (on peut voir/toucher le serveur)

## Modifications du code backend

### Leader election

Remplacer l'advisory lock PostgreSQL par une verification du role Patroni :

```python
# Avant : pg_try_advisory_lock sur une DB partagee
# Apres : demander a Patroni si ce noeud est primary

async def leader_election_loop():
    while True:
        try:
            # Patroni expose une API REST sur le port 8008
            r = requests.get("http://patroni:8008/primary", timeout=2)
            if r.status_code == 200:
                is_leader.set()
            else:
                is_leader.clear()
        except:
            is_leader.clear()
        await asyncio.sleep(10)
```

### Failover app (Android)

L'app doit connaitre les 3 URLs backend :

```kotlin
private val BACKEND_URLS = listOf(
    BuildConfig.BACKEND_URL_1,  // baie site (reseau local)
    BuildConfig.BACKEND_URL_2,  // datacenter A
    BuildConfig.BACKEND_URL_3,  // datacenter B
)
```

Failover : apres 3 echecs consecutifs, passer a l'URL suivante (rotation).

## Estimation des couts

| Noeud | Hebergement | Cout mensuel |
|-------|-------------|-------------|
| NODE1 | Serveur physique dans la baie | 0 EUR (electricite existante) |
| NODE2 | VPS cloud DC A (4 vCPU, 4 Go RAM) | ~15 EUR/mois |
| NODE3 | VPS cloud DC B (4 vCPU, 4 Go RAM) | ~15 EUR/mois |
| **Total** | | **~30 EUR/mois** |

## Comparaison avec Option A (SQLite sync)

| Critere | Option A | Option B (3 noeuds) |
|---------|----------|---------------------|
| Cout | 0 EUR de plus | +30 EUR/mois |
| Perte de donnees possible | 30s de fenetre | Zero (replication synchrone) |
| Split-brain | Theoriquement possible | Impossible (quorum) |
| Survie panne 1 noeud | Oui | Oui |
| Survie panne 1 datacenter | Non (2 DC seulement) | Oui (3 sites distincts) |
| Fonctionnement site isole | Oui (lecture + ecriture) | Lecture seule (pas de quorum) |
| Complexite setup | Faible | Moyenne (Patroni + etcd) |
| Complexite maintenance | Faible | Moyenne |
| Code source identique partout | N/A (2 composes differents) | Oui (1 compose + .env) |
