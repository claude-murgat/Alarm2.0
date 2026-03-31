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

### P2 — Dead man's switch externe (filet de sécurité ultime)
- **Pourquoi** : si les deux backends tombent simultanément (ou la DB managée), l'app est silencieuse sans que personne ne le sache
- **Comment** : chaque backend ping Healthchecks.io toutes les 5 min ; si silence des DEUX → SMS automatique
- **Coût** : gratuit (Healthchecks.io plan free)
- **Rôle** : dernier filet uniquement — ne se déclenche que dans le scénario catastrophe (les deux VPS + DB simultanément HS)

### P3 — SMS/appel de secours (Twilio ou OVH SMS)
- **Pourquoi** : indépendant de l'app Android (si app tuée par OS, SMS passe quand même)
- **Déclenchement** : alarme non acquittée après N minutes → SMS envoyé par le backend via API Twilio
- **Flux** : backend cloud → HTTPS vers api.twilio.com → réseau SS7/téléphonie → SMS sur téléphone astreinte
  - Le téléphone n'a besoin que du signal GSM/SMS, **pas de data internet**
- **Implémentation** : 3 lignes Python dans la boucle d'escalade existante (`twilio.rest.Client.messages.create()`)
  - Colonne `phone_number` à ajouter dans la table `users`
  - Variables d'env : `TWILIO_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`
  - Numéro loué chez Twilio : ~1€/mois ; SMS sortant France : ~0.08€/SMS (~0.24€ par incident à 3 personnes)
- **Protège contre** : app Android tuée par OS, téléphone sans données internet mais avec signal voix/SMS
- **Limite** : si les deux backends sont morts → les SMS ne peuvent plus partir (c'est le backend qui appelle Twilio)
  - C'est pour ça que P2 (dead man's switch) reste le filet ultime

### P4 — UPS dédié Starlink (si pas déjà fait)
- S'assurer que l'antenne Starlink a sa propre batterie de secours
- Indépendant du double secours du site pour maximiser la résilience

## Matrice de résilience finale (P1+P2+P3 implémentés)

| Scénario | Impact | Couvert par |
|---|---|---|
| Crash process backend | Bascule ~10s, pleine fonctionnalité | P1 (VPS2) |
| Panne hardware VPS1 | Bascule ~10s, pleine fonctionnalité | P1 (VPS2) |
| Incendie datacenter VPS1 | Bascule ~10s, pleine fonctionnalité | P1 (VPS2, datacenter différent) |
| Les 2 VPS morts simultanément | Mode dégradé SMS | P2 (dead man) + P3 (SMS direct) |
| App Android tuée par l'OS | SMS reçu même sans data | P3 (Twilio → GSM) |
| Fibre + mobile coupés | Starlink prend le relais | Infrastructure existante |
| Coupure courant totale | Sirène locale uniquement | Limites physiques irréductibles |

## Ce que l'infrastructure ne couvrira jamais
- Coupure simultanée courant + toutes connectivités → sirène locale uniquement
- Équipe entière indisponible (accident collectif, grève, etc.) → process RH/organisationnel
- Erreur humaine volontaire (ack sans agir) → confiance + re-sonnerie 30 min
