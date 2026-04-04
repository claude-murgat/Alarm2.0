# Option A — Deux backends autonomes SQLite + sync applicative

## Contexte

- 10 utilisateurs, 5 alarmes/nuit, 1 seule alarme active a la fois
- 2 VPS (VPS1 primaire, VPS2 secondaire)
- Objectif : survie complete si un VPS tombe

## Architecture

```
VPS1                                    VPS2
+---------------+                      +---------------+
|   Backend     |                      |   Backend     |
|   SQLite      |  <-- sync alarm -->  |   SQLite      |
|   (fichier)   |  <-- sync users -->  |   (fichier)   |
+---------------+                      +---------------+
```

- Chaque VPS a sa propre DB SQLite locale (un fichier, zero dependance reseau)
- Pas de PostgreSQL, pas de streaming replication, pas de standby, pas de promotion
- Les users/escalation config sont seeds identiques des deux cotes
- La sync se fait via HTTP POST best-effort entre les deux backends

## Ce qui est synchronise

| Donnee | Mecanisme | Frequence |
|--------|-----------|-----------|
| Alarme active (create/ack/resolve/escalate) | POST /api/sync/alarm apres chaque mutation | ~30-40 fois/nuit |
| Users online | POST /api/sync/online-users | Toutes les 10s |
| Config (users, escalation chain) | Seed identique au demarrage | Une fois |
| Heartbeats | Pas synchronises — chaque VPS recoit les siens | - |
| SMS queue | Pas synchronisee — seul le primary envoie | - |

## Leader election sans PostgreSQL

Le primary est le VPS qui recoit des heartbeats des apps.
Les apps sont toutes connectees au meme VPS (failover app bascule les 10 ensemble).
Le VPS qui recoit des heartbeats execute l'escalade, l'autre dort.

```python
# Dans escalation_loop :
if not received_heartbeat_in_last_60s():
    await asyncio.sleep(10)
    continue
# J'ai des heartbeats -> je suis primary, j'escalade
```

## Sync alarme — 5 points d'appel

1. POST /api/alarms/send — alarme creee
2. POST /api/alarms/{id}/ack — alarme acquittee
3. POST /api/alarms/{id}/resolve — alarme resolue
4. escalation.py — alarme escaladee (boucle naturelle)
5. POST /api/test/send-alarm — alarme de test

Chaque mutation : POST best-effort vers le peer avec l'etat complet de l'alarme.
Filet de securite : sync periodique toutes les 30s (GET /api/sync/alarm sur le peer).

## Sync online-users

Necessaire avec 10 utilisateurs pour eviter que l'escalade skip un user
qui est en ligne sur l'autre VPS.

```
Toutes les 10s :
VPS1 -> VPS2 : {"online_user_ids": [1, 2, 3, 5, 7, 8, 9]}
VPS2 -> VPS1 : {"online_user_ids": [4, 6]}
```

Chaque VPS merge les deux listes : un user est "online" s'il est vu
par l'un OU l'autre VPS.

## Analyse des risques de desynchronisation

### Scenario 1 : Peer down pendant sync alarme

```
t=0  VPS1 cree l'alarme -> sync vers VPS2 -> ECHEC (VPS2 down)
t=5  VPS1 tombe
t=6  VPS2 revient -> PAS D'ALARME dans sa DB
```

- Risque : PERTE D'ALARME
- Probabilite : Faible (les 2 VPS down au meme moment)
- Mitigation : sync periodique 30s rattrape si VPS2 revient avant VPS1 tombe
- Fenetre de perte : 30s max
- En pratique : quasi nulle (qui envoie l'alarme si les 2 VPS sont down ?)

### Scenario 2 : Ecriture concurrente sur les deux VPS

```
t=0  Reseau coupe entre VPS1 et VPS2
t=1  User1 sur VPS1 acquitte l'alarme
t=1  User2 sur VPS2 acquitte l'alarme
t=5  Reseau revient -> conflit : deux acks differents
```

- Risque : CONFLIT D'ETAT
- Probabilite : Tres faible (split brain + action simultanee)
- Mitigation : last-write-wins avec timestamp (updated_at)
- Risque residuel : Negligeable

### Scenario 3 : Collision d'IDs alarme

```
VPS1 cree alarme id=42 (auto-increment)
VPS2 cree alarme id=42 (auto-increment)
```

- Risque : COLLISION
- Mitigation : seul le primary (celui qui recoit les heartbeats) cree des alarmes
  OU utiliser des UUIDs

### Scenario 4 : Double escalade

```
Les deux VPS executent la boucle d'escalade en meme temps
-> double SMS, double notification
```

- Risque : ACTIONS DUPLIQUEES
- Mitigation : seul le VPS avec heartbeats escalade (leader implicite)
  + guard anti-doublon SMS existant

### Scenario 5 : Split brain (2 apps sur VPS2, 8 sur VPS1)

- Risque : L'escalade sur VPS1 skip les 2 users qu'il croit offline
- Mitigation : sync online-users toutes les 10s, merge des listes

## Resume des risques

| Scenario | Probabilite | Impact | Mitigation | Risque residuel |
|----------|-------------|--------|------------|-----------------|
| Peer down pendant sync | Faible | Alarme perdue | Sync periodique 30s | ~30s fenetre |
| Ecriture concurrente | Tres faible | Conflit d'etat | Last-write-wins | Negligeable |
| Collision IDs | Possible | Corruption | Primary-only ou UUID | Elimine |
| Double escalade | Faible | Double SMS | Leader implicite + dedup | Elimine |
| Split brain users | Moyen (10 users) | Escalade incorrecte | Sync online-users 10s | Elimine |

## Ce qu'on supprime par rapport a l'architecture actuelle

- PostgreSQL primary (docker service db)
- PostgreSQL standby (db-standby + db-standby-init)
- Streaming replication, WAL, replicator user
- Advisory lock pour leader election
- docker-compose.vps2.yml passe de 3 services a 1 seul (backend)

## Docker simplifie

```yaml
# docker-compose.vps2.yml
services:
  backend:
    build: ./backend
    ports:
      - "8001:8000"
    environment:
      - DATABASE_URL=sqlite:///data/alarm.db
      - PEER_URL=http://vps1:8000
    volumes:
      - backend_data:/app/data
```
