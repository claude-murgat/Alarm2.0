# Notes de sécurité du site industriel — Alarm 2.0

> Référence interne — mise à jour au fil des discussions

## Infrastructure physique existante

| Élément | Statut | Notes |
|---------|--------|-------|
| Alimentation électrique | ✅ Double secouru | Tout le site est redondé électriquement |
| Onduleurs (UPS) | ✅ En place | Tous les serveurs sont sur onduleurs |
| Alarme câblée PLC | ✅ En place | Câblée directement sur les automates |
| Alarme locale (sirène/voyant) | ✅ En place | Sur site, indépendante du réseau |
| Fibre optique | ✅ Lien principal | Déjà tombé (chute de neige) |
| Starlink | ✅ En redondance | Backup de la fibre, nécessite courant (secouru) |
| Réseau mobile 4G/5G | ⚠️ Dépend opérateur | Peut saturer ou tomber lors de gros événements |
| Présence humaine nuit | ❌ Aucune | Site non gardé la nuit → astreinte à distance obligatoire |

## Politique d'astreinte actuelle (Alarm 2.0)

- **Escalade** : user1 (pos.1) → user2 (pos.2) → admin (pos.3) → rebouclage
- **Délai d'escalade** : 15 min par palier (configurable)
- **Escalade cumulative** : tout le monde continue de sonner, n'importe qui peut acquitter
- **Acquittement** : suspend 30 min, puis re-sonne si non résolu
- **Alarme ack visible** : les autres notifiés voient "Acquittée par X" avec countdown
- **Watchdog** : si l'app de l'astreinte (pos.1) perd le heartbeat > 15 min → alarme auto vers pos.2
- **Perte réseau app** : sonnerie continue après 2 min sans heartbeat
- **Chaîne vide / tout offline** : email direction_technique@charlesmurgat.com

## Scénarios de panne analysés

### Chute de neige (fibre + mobile coupés) — déjà arrivé
- Fibre : coupée ❌
- Mobile : potentiellement saturé/coupé ❌
- Starlink : **fonctionne** (alimentation secouru) ✅
- Backend sur site : **accessible** via Starlink ✅
- App Android : **fonctionne** via Starlink (si téléphone astreinte a du réseau Starlink/mobile)
- → Risque résiduel : téléphone de l'astreinte sans aucun réseau (hors zone Starlink)

### Coupure courant totale (groupe de secours KO)
- Double alimentation : **censée tenir** ✅
- Si les deux tombent : Starlink HS, backend HS, alarme câblée PLC HS
- → Seule la sirène locale résiste (si batterie propre)
- → Risque très faible (double secours), mais non nul

### Défaillance backend (panne hardware serveur)
- Backend sur site : ❌ mort
- App Android : ne reçoit plus rien, sonnerie "perte heartbeat" après 2 min
- → L'astreinte est alertée par la sonnerie de perte de connexion, mais ne sait pas POURQUOI
- → **Point de défaillance unique identifié**

### Défaillance backend cloud (VPS)
- Le fournisseur cloud a des onduleurs, du RAID, de la supervision — mais pas de garantie zéro
- Exemple réel : incendie OVH SBG2 (mars 2021) → destruction totale d'un datacenter, aucun préavis
- SLA typique 99,9% = 8h de downtime/an toléré contractuellement
- **Ce que ça change vs on-site** : c'est le fournisseur qui gère le hardware (pas nous), mais la probabilité zéro n'existe pas
- → Mitigation : dead man's switch (P2) + restart Docker automatique

## Trous identifiés et couverture

| # | Risque | Couvert ? | Par quoi |
|---|--------|-----------|----------|
| 1 | Backend tombe (hardware) | ⚠️ Partiel | Sonnerie perte heartbeat, mais pas d'info sur l'alarme industrielle |
| 2 | Connectivité totale perdue (fibre+mobile+Starlink) | ❌ Non | Seule sirène locale |
| 3 | PLC ne peut pas atteindre backend (réseau interne) | ⚠️ Selon archi | Dépend de comment le PLC envoie les alarmes |
| 4 | Téléphone astreinte HS (batterie, app tuée) | ✅ Oui | Escalade (pas d'ack → +15 min → user2) + alarme astreinte si heartbeat perdu |
| 5 | Ack délibéré sans intervention | ⚠️ Partiel | Re-sonne après 30 min, mais délai accepté |
| 6 | Toute l'équipe indisponible | ⚠️ Partiel | Email direction_technique, mais si réseau KO → rien |

## Améliorations recommandées (priorisées)

### P1 — Backend cloud redondant (2 VPS + réplication PostgreSQL)
- **Pourquoi** : maintenir la pleine fonctionnalité même en cas de panne hardware d'un backend
  - SMS/dead man's switch seuls = mode dégradé (perte des fonctionnalités app)
  - Doublon backend = bascule transparente en ~10s, fonctionnalité complète maintenue
- **Attention** : 2 VPS + DB managée partagée (Supabase) déplace simplement le SPOF sur la DB
  - Si Supabase tombe → les deux VPS perdent leur DB simultanément → pire qu'un seul VPS
  - La vraie solution : **chaque VPS a sa propre PostgreSQL, synchronisées par streaming replication**
- **Architecture recommandée** :
  - VPS1 (OVH Paris) : FastAPI + PostgreSQL PRIMARY → écrit et lit localement
  - VPS2 (Scaleway Amsterdam) : FastAPI + PostgreSQL REPLICA → reçoit le WAL en continu (~ms de lag)
  - En cas de mort de VPS1 : app bascule sur VPS2 (~10s) + promotion du replica en primary (1 commande)
  - La boucle d'escalade utilise un verrou PostgreSQL advisory (`pg_try_advisory_lock`) → un seul backend escalade à chaque tick
- **SPOF résiduel** : les deux datacenters tombent simultanément → probabilité infime
- **Split-brain** (lien VPS1↔VPS2 coupé mais les deux vivants) : les deux bases divergent temporairement mais **aucune ne tombe** — resynchronisation à la reconnexion
- **Starlink** : câbler en parallèle (pas en série derrière le modem principal) → le modem n'est plus un SPOF
- **Coût** : ~8 €/mois (VPS1 ~3,50€ + VPS2 ~3,60€ — pas de DB externe payante)

### P2+P3 — Machine on-site : gateway SMS + appels vocaux + health monitor
- **Hardware retenu** : Mini PC business (Lenovo/HP/Dell) + Waveshare SIM7600E-H 4G HAT (~70€) + SIM Free 2€/mois
- **Détail complet** : voir `ARCHITECTURE_SMS_VOIX.md`
- **Ancienne proposition** : clé USB GSM Huawei E3372 → abandonnée (2G/3G obsolète, pas de voix/TTS)
- **Triple rôle sur le nœud on-site (NODE1 du cluster Patroni)** :

  **Rôle 1 — Gateway SMS + appels vocaux (alarmes)**
  - Poll `GET /internal/sms/pending` + `GET /internal/calls/pending` sur le backend local (ou VPS cloud en fallback)
  - Envoie les SMS et passe les appels vocaux via le Waveshare SIM7600E-H (pyserial + AT commands)
  - Appels avec TTS (AT+CTTS) + acquittement DTMF (décodage Goertzel logiciel sur le port audio USB)
  - Aucun port entrant à ouvrir sur le réseau site

  **Rôle 2 — Health monitor**
  - Poll `GET /health` sur les 3 nœuds (NODE1, NODE2, NODE3) toutes les 5 min
  - Si les TROIS ne répondent plus → envoie SMS d'alerte directement via le SIM7600
  - L'endpoint `/health` vérifie DB joignable + boucle d'escalade active

  **Rôle 3 — Failover internet 4G**
  - Le SIM7600 sert aussi de connexion internet de secours (mode ECM, métrique 300)
  - Si fibre + Starlink tombent → le nœud on-site reste connecté au cluster via 4G
  - Les push FCM et la réplication PostgreSQL transitent par la 4G

- **Implémentation backend** : tables `sms_queue` + `call_queue` dans PostgreSQL (répliquées sur les 3 nœuds Patroni)
  - La boucle d'escalade écrit dans `sms_queue` / `call_queue`
  - Endpoints : `GET /internal/sms/pending`, `GET /internal/calls/pending`, `POST /internal/calls/{id}/result`
- **Coût** : ~70€ une fois (Waveshare SIM7600E-H) + ~2€/mois (SIM Free) — zéro dépendance cloud pour les notifications
- **SPOF résiduel** : le nœud on-site tombe → plus de SMS/appels vocaux
  - Acceptable : les push FCM continuent via les VPS cloud, le cluster Patroni reste fonctionnel (2/3 nœuds)
  - La sirène câblée PLC reste active indépendamment

### P4 — UPS dédié Starlink (si pas déjà fait)
- S'assurer que l'antenne Starlink a sa propre batterie de secours
- Indépendant du double secours du site pour maximiser la résilience

## Matrice de résilience finale (P1+P2+P3 implémentés)

| Scénario | Impact | Couvert par |
|---|---|---|
| Crash process backend | Bascule ~10s, pleine fonctionnalité | P1 (VPS2) |
| Panne hardware VPS1 | Bascule ~10s, pleine fonctionnalité | P1 (VPS2) |
| Incendie datacenter VPS1 | Bascule ~10s, pleine fonctionnalité | P1 (VPS2, datacenter différent) |
| Les 2 VPS morts simultanément | SMS d'alerte + mode dégradé | Machine on-site (health monitor → GSM) |
| App Android tuée par l'OS | SMS reçu même sans data | Machine on-site (gateway → GSM) |
| Fibre + mobile coupés | Starlink prend le relais | Infrastructure existante |
| Coupure courant totale | Sirène locale uniquement | Limites physiques irréductibles |

## Ce que l'infrastructure ne couvrira jamais
- Coupure simultanée courant + toutes connectivités → sirène locale uniquement
- Équipe entière indisponible (accident collectif, grève, etc.) → process RH/organisationnel
- Erreur humaine volontaire (ack sans agir) → confiance + re-sonnerie 30 min
