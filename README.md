# Système de Gestion d'Alarmes Critiques (Alarm 2.0)

Système d'astreinte avec notification mobile, sonnerie continue, acquittement temporaire, watchdog, escalade automatique et persistence PostgreSQL.

## Architecture

```
┌─────────────────────────┐         ┌──────────────────────────────────┐
│  App Android (Kotlin)    │◄──────►│  FastAPI Backend (Docker)        │
│  ├─ Login par nom        │  HTTP   │  ├─ API REST                    │
│  ├─ Mode astreinte :     │ polling │  ├─ Interface web admin (/)      │
│  │  ├─ Polling /mine 3s  │  3s     │  ├─ Moteur d'escalade (asyncio) │
│  │  ├─ Heartbeat 3s      │         │  ├─ Watchdog (30s)              │
│  │  └─ Sonnerie alarme   │         │  ├─ Horloge injectable (tests)  │
│  ├─ Mode veille :        │         │  ├─ Email SMTP (Mailhog)        │
│  │  ├─ FCM uniquement    │  push   │  └─ FCM (Firebase Cloud Msg)    │
│  │  └─ Reveil par push   │         └────────────┬─────────────────────┘
│  ├─ Acquittement         │                      │
│  └─ Refresh token        │         ┌────────────┼──────────────┐
└──────────────────────────┘         │            │              │
                                PostgreSQL    Mailhog       Docker
                                  :5432        :8025        Compose
```

## Mécanismes d'alarme — Vue complète

### 1. Cycle de vie d'une alarme

```
                    ┌──────────────────────────────────────────────────┐
                    │                                                  │
                    ▼                                                  │
  ┌──────────┐  Envoi  ┌──────────┐  Acquittement  ┌──────────────┐  │  Expiration 30min
  │          │────────►│  ACTIVE  │──────────────►│  ACQUITTÉE   │──┘  (réactivation auto)
  │  (rien)  │         │  🔴      │                │  ✅ 30min    │
  │          │         └────┬─────┘                └──────────────┘
  └──────────┘              │                             │
                            │ Résolution                  │ Résolution pendant suspension
                            ▼                             ▼
                      ┌──────────┐                  ┌──────────┐
                      │ RÉSOLUE  │                  │ RÉSOLUE  │
                      │ (histo)  │                  │ (histo)  │
                      └──────────┘                  └──────────┘
```

**Règles :**
- **Une seule alarme active à la fois** (HTTP 409 si on en envoie une 2e)
- **Acquittement** = arrêt de la sonnerie pour celui qui acquitte + suspension 30 min
- **Expiration de l'acquittement** = l'alarme redevient ACTIVE (sonnerie reprend)
- **Résolution** = alarme terminée, passe dans l'historique
- **Historisation** : qui a acquitté (nom) + quand
- **Alarme acquittée visible par tous les notifiés** : les autres utilisateurs voient l'alarme
  avec le statut "Acquittée par [nom]" — pas de son, pas de bouton ack
- **Countdown dynamique** : le temps restant de la suspension est mis à jour à chaque poll
  (ex: "29 min restantes", "28 min restantes"…)
- **Nouvelle alarme = nouvelle sonnerie** : si une alarme est résolue puis qu'une nouvelle
  arrive, elle sonne même si l'utilisateur avait acquitté la précédente (reset par ID)

### 2. Modes astreinte / veille

Le systeme distingue deux profils avec des impacts batterie differents :

```
  ┌──────────────────────────────────────────────────────────┐
  │  MODE ASTREINTE (position 1 — telephone pro sur secteur) │
  │  ├─ Foreground service permanent                         │
  │  ├─ Heartbeat POST toutes les 3s                         │
  │  ├─ Polling GET /alarms/mine toutes les 3s               │
  │  ├─ Sonnerie immediate si alarme                         │
  │  └─ Delai escalade : 15 min (configurable)               │
  └──────────────────────────────────────────────────────────┘

  ┌──────────────────────────────────────────────────────────┐
  │  MODE VEILLE (positions 2+ — telephones perso)           │
  │  ├─ AUCUN foreground service au repos                    │
  │  ├─ AUCUN heartbeat, AUCUN polling                       │
  │  ├─ Seul FCM actif (cout batterie ~0, via Play Services) │
  │  ├─ Reveil par push FCM quand l'escalade arrive          │
  │  ├─ Delai escalade : 2 min (si pas d'ack apres FCM)     │
  │  └─ Service s'arrete automatiquement apres resolution    │
  └──────────────────────────────────────────────────────────┘
```

Le champ `is_oncall` dans la reponse de login indique le mode (position 1 = true).

### 3. Escalade cumulative

```
  Alarme envoyee
       │
       ▼
  ┌─────────┐  15 min sans ack  ┌─────────┐  2 min   ┌─────────┐
  │  user1   │─────────────────►│  user2   │────────►│  admin   │
  │ (pos. 1) │  🔊 SONNE        │ (pos. 2) │  🔊+FCM  │ (pos. 3) │
  │ ASTREINTE│  TOUJOURS        │ VEILLE   │  AUSSI   │ VEILLE   │
  │ 🔊 SONNE │                  │ 🔊 SONNE │          │ 🔊 SONNE │
  └─────────┘                   └─────────┘          └────┬─────┘
                                                          │ 2 min
                                                          ▼
                                                   Rebouclage

  ★ CUMULATIVE : chaque utilisateur appele CONTINUE de sonner.
    N'importe lequel peut acquitter l'alarme.
  ★ FCM : les users en veille sont reveilles par push notification.
  ★ 2 PALIERS : astreinte (pos 1) = 15 min, veille (pos 2+) = 2 min.
```

**Regles :**
- **Delai 2 paliers** : astreinte (position 1) = 15 min, veille (positions 2+) = 2 min
- **Plus de filtre online** : l'escalade suit l'ordre de la chaine, le FCM reveille les users en veille
- **Escalade cumulative** : les utilisateurs precedents continuent de voir/entendre l'alarme
- **N'importe qui** parmi les notifies peut acquitter (pas seulement le dernier)
- **Liste des notifies** visible (`notified_user_ids` + `notified_user_names`)
- **Rebouclage** apres le dernier
- **Pas d'escalade** si alarme acquittee
- **Reprise d'escalade** si ack expire et alarme redevient active
- **Chaine vide** → email a `direction_technique@charlesmurgat.com` (configurable)
- **Timing teste** via horloge injectable (pas de sleep dans les tests)

### 3b. Utilisateur d'astreinte hors connexion

```
  User #1 (astreinte contractuelle, position 1)
       │
       │ Heartbeat perdu depuis > 15 min
       ▼
  ┌─────────────────────────────────────────────────┐
  │ ALARME AUTO : "Utilisateur d'astreinte hors     │
  │                connexion (user1)"               │
  │ → Assignée à user #2 (solidarité)              │
  │ → Escalade normale si non acquittée            │
  │ → Auto-résolue si user #1 revient en ligne     │
  └─────────────────────────────────────────────────┘
       │
       │ Si PERSONNE n'est connecté
       ▼
  📧 Email direction_technique@charlesmurgat.com
```

**Règles :**
- **Seul le #1** (position 1, astreinte contractuelle) est surveillé
- Les autres positions (solidarité) ne déclenchent PAS d'alarme s'ils se déconnectent
- Le délai avant alerte est de **15 minutes** d'absence
- L'alarme d'astreinte suit l'**escalade normale** (user2 → admin → rebouclage)
- **Auto-résolution** si user #1 revient en ligne (heartbeat)
- Si **personne** n'est connecté → email à la direction technique

### 4. Watchdog / Heartbeat (mode astreinte uniquement)

```
  App Android                        Backend
  ┌──────────┐    POST /heartbeat    ┌──────────┐
  │ toutes   │──────────────────────►│ MAJ      │
  │ les 3s   │    200 OK             │ timestamp│
  └──────────┘◄──────────────────────└──────────┘
                                          │
                                     Watchdog loop (30s)
                                          │
                                     Si heartbeat > 60s
                                          │
                                          ▼
                                    is_online = false

  Côté App :
  ┌──────────────────────────────────────────────────┐
  │ Heartbeat OK        → ✅ Connexion serveur ok     │
  │ Heartbeat KO        → ❌ Déconnecté               │
  │ KO depuis > 2 min   → ⚠️ + SONNERIE CONTINUE     │
  │ KO puis revient OK  → ✅ (sonnerie arrêtée)       │
  └──────────────────────────────────────────────────┘
```

**Côté serveur (front web) :**
- Bouton "Simuler perte connexion" = toggle pause du heartbeat
- Quand en pause : endpoint retourne 503 → l'app détecte la perte
- Quand repris : heartbeat normal reprend

### 5. Authentification et Token

```
  Login (nom + mdp)
       │
       ▼
  JWT Token (24h)
       │
       ├─► Polling /alarms/mine (Bearer token)
       ├─► Heartbeat (Bearer token)
       │
       │  Toutes les 12h :
       ├─► POST /auth/refresh → nouveau token
       │
       │  Si refresh échoue (401) :
       └─► SONNERIE CONTINUE + message permanent
           "Votre session a expiré..."
```

### 6. Suppression d'utilisateur

```
  Utilisateur supprimé pendant alarme active
       │
       ▼
  Alarme réassignée au suivant dans la chaîne d'escalade
  (ou au premier autre utilisateur si chaîne vide)
```

### 7. Persistence

```
  PostgreSQL (Docker volume pgdata)
       │
       ├─► Utilisateurs, alarmes, config escalade
       ├─► Survit au restart du backend
       └─► Survit au restart Docker
```

## Prérequis

- **Docker Desktop** (`winget install Docker.DockerDesktop`) + WSL2
- **JDK 17** (`winget install EclipseAdoptium.Temurin.17.JDK`)
- **Android SDK** avec platform-tools, build-tools 34, emulator, system-images

## Démarrage rapide

### 1. Backend (Docker Compose — PostgreSQL + Mailhog)

```bash
docker compose up --build -d
# Backend : http://localhost:8000
# Mailhog : http://localhost:8025
```

### 2. App Android (Émulateur)

```bash
# Build + install
cd android && ./gradlew assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
adb reverse tcp:8000 tcp:8000
```

Ou en un clic : **double-cliquer sur `launch.bat`** à la racine du projet.

## Comptes de test

| Nom | Mot de passe | Rôle | Escalade |
|-----|-------------|------|----------|
| admin | admin123 | Admin | Position 3 |
| user1 | user123 | Utilisateur | Position 1 |
| user2 | user123 | Utilisateur | Position 2 |

## Tests E2E

### Backend (62 tests — nécessite Docker Compose up)

```bash
python -m pytest tests/test_e2e.py -v
```

| Suite | Tests | Couverture |
|-------|-------|-----------|
| Health & Web UI | 2 | Serveur up, interface chargée |
| Login | 7 | Admin, case-insensitive, invalide, espaces rejetés, stockage lowercase |
| Alarme unique | 4 | Envoi, 409 si doublon, envoi après résolution, réception user |
| Acquittement | 5 | Statut, suspension visible + acknowledged, historisation nom, expiration → réactivation, reprise escalade |
| Escalade manuelle | 6 | Chaîne complète, skip offline, rebouclage, wrap continu, pas si ack, pas si vide |
| Escalade timing | 3 | Pas avant délai, après délai, à la limite exacte (horloge injectable) |
| Watchdog | 3 | Heartbeat, détection offline, affichage récent en secondes |
| Interface web | 6 | Alarme test, watchdog, perte connexion, reset, statut, toggle pause |
| Skip offline | 2 | Saute utilisateur offline, wrap vers premier online |
| Utilisateur supprimé | 1 | Réassignation alarme |
| Chaîne vide + email | 2 | Email envoyé, destinataire configurable |
| Résilience | 6 | Alarme inexistante, champs vides, token invalide |
| Token refresh | 1 | Endpoint /auth/refresh |
| Persistence Docker | 1 | Données survivent au restart |
| Email SMTP (Mailhog) | 1 | Email réel capturé dans Mailhog |
| Escalade cumulative | 3 | Alarme visible par tous les notifiés, n'importe qui peut ack, liste des notifiés |
| Astreinte hors connexion | 5 | Alarme auto après 15min, auto-résolution, escalade, pas pour #2+, email si personne |
| Visibilité notifiés | 1 | notified_user_names dans la réponse API |
| Visibilité alarme acquittée | 3 | Alarme ack visible par autres notifiés, countdown ack_remaining_seconds, visible par l'acker aussi |

### Android Espresso (21 tests — aucun backend nécessaire)

```bash
cd android && ./gradlew connectedAndroidTest
```

| # | Test | Vérifie |
|---|------|---------|
| 01 | Pas d'alarme → ligne inactive | ⚪ + bouton ack caché |
| 02 | Alarme active avec durée | 🔴 + titre + "depuis X min" |
| 03 | Acquittement → statut + temps restant | ✅ Acquittée + "30 min restantes" |
| 04 | Login depuis état déconnecté | Saisie nom + mdp → dashboard |
| 05 | Heartbeat OK → icône verte | ✅ |
| 06 | Heartbeat KO → icône rouge | ❌ |
| 07 | Historique alarmes passées | Section historique visible |
| 08 | Alarme terminée → retour inactif | ⚪ après disparition alarme |
| 09 | Perte heartbeat > timeout → alerte | ⚠️ Connexion perdue + sonnerie |
| 10 | Heartbeat revient avant timeout | Pas d'alerte |
| 11 | Alarme resonne après expiration ack | 🔴 revient après suspension |
| 12 | Alarme ne resonne pas si résolue | ⚪ stable |
| 13 | Rotation bloquée + état préservé | Portrait forcé |
| 14 | Déconnexion puis reconnexion | Cycle complet |
| 15 | Alarme reçue en arrière-plan | Service foreground capte l'alarme |
| 16 | Refresh token automatique | Nouveau token dans SharedPrefs |
| 17 | Échec refresh → sonnerie + message | ⚠️ permanent + son continu |
| 18 | Escalade visible (alarme disparaît) | ⚪ quand escaladée |
| 19 | Alarme ack par autre → affichage info | "Acquittée par X", pas de bouton, pas de son |
| 20 | Countdown mis à jour à chaque poll | "30 min" → "29 min" |
| 21 | Nouvelle alarme après résolution sonne | 🔴 + bouton ack reset même si ack précédent |

**Total : 83 tests E2E (62 backend + 21 mobile)**

## Stack technique

| Composant | Technologie |
|-----------|-------------|
| Backend | FastAPI + SQLAlchemy + PostgreSQL 16 |
| Frontend web | HTML/JS vanilla (SPA intégrée au backend) |
| App mobile | Android natif Kotlin (Retrofit, Coroutines) |
| Infra | Docker Compose (backend + PostgreSQL + Mailhog) |
| Tests backend | pytest + requests (contre backend live) |
| Tests mobile | Espresso + FakeApiService (isolé, sans backend) |
| DI mobile | Manuel (ApiProvider singleton) |
| Email | SMTP via Mailhog (dev) / configurable (prod) |
| Push | FCM (Firebase Cloud Messaging) pour reveil mode veille |

## Troubleshooting

- **Docker ne démarre pas** : Nécessite WSL2 + reboot après installation
- **bcrypt error** : `pip install bcrypt==4.0.1`
- **App ne se connecte pas** : `adb reverse tcp:8000 tcp:8000`
- **Tests flaky** : Fermer les émulateurs/navigateurs qui envoient des heartbeats pendant les tests

## Limitations actuelles

- Pas de HTTPS/TLS (developpement uniquement)
- L'alarme d'astreinte ne peut pas coexister avec une alarme manuelle (contrainte alarme unique)
- Phase B prevue : SSE (Server-Sent Events) pour push temps reel sans polling
