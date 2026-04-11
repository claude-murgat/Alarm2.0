# Prompt Claude Code — Implementation SSE + FCM (Points 10 & 17)

## Contexte projet

Systeme d'alarmes critiques : backend FastAPI + app Android + cluster PostgreSQL HA (Patroni 3 noeuds).
Process TDD strict : RED (tests d'abord) → validation utilisateur → GREEN (implementation).
Regles detaillees dans `.claude/CLAUDE.md`.

## Ce qui existe deja

- **Polling 3s** : `AlarmPollingService.kt` fait `GET /alarms/mine` + `POST /devices/heartbeat` toutes les 3s
- **Events** : `events.py` log les evenements en JSON (`log_event("alarm_escalated", ...)`) mais ne publie nulle part
- **Email** : `email_service.py` stocke le dernier email dans `_last_email` + envoi SMTP optionnel (pattern a reproduire pour FCM)
- **FakeApiService** : mock Espresso complet dans `androidTest/`, utilise `PollingIdlingResource`
- **ApiService.kt** : interface Retrofit, toutes les routes declarees
- **Mailhog** : deja dans docker-compose pour les tests email SMTP
- **Escalade** : `_find_next_online_user()` dans `escalation.py` saute les users offline (is_online=False)

## Architecture deux modes (IMPORTANT)

Le systeme distingue deux profils d'utilisateur avec des impacts batterie tres differents :

### Mode Astreinte (position 1 — telephone pro dedie)

L'utilisateur d'astreinte a un telephone branche sur secteur, la batterie n'est pas un sujet.
- Foreground service permanent (`AlarmPollingService`)
- Heartbeat POST toutes les 3s (inchange)
- Connexion SSE permanente pour recevoir les events temps reel
- Polling `GET /alarms/mine` en fallback si SSE deconnecte > 5s
- FCM en complement pour reveil si Android tue le service

### Mode Veille (positions 2, 3, etc. — telephones perso de collegues volontaires)

Ces utilisateurs ne sont PAS d'astreinte. Ils interviennent par solidarite quand l'escalade arrive jusqu'a eux. Impact batterie = quasi zero.
- AUCUN foreground service au repos
- AUCUN heartbeat
- AUCUNE connexion SSE
- AUCUN polling
- Seul FCM est actif (maintenu par Google Play Services, cout negligeable — identique a WhatsApp/Signal)
- Quand l'escalade les atteint : le backend envoie un FCM high-priority → Android reveille l'app → `AlarmWakeUpHandler` demarre le foreground service → sonnerie + SSE + heartbeat s'activent
- Une fois l'alarme resolue : le foreground service s'arrete, retour en mode veille

### Impact sur la logique d'escalade

Changement majeur : `_find_next_online_user()` dans `escalation.py` doit devenir `_find_next_user()`.

**Avant** : l'escalade saute les users offline (pas de heartbeat = offline = ignore).
**Apres** : l'escalade suit l'ordre de la chaine sans verifier `is_online`. Le FCM se charge du reveil.

Concretement :
- Supprimer le filtre `is_online` dans `_find_next_online_user()` → renommer en `_find_next_user()`
- Quand l'escalade assigne un nouvel utilisateur : envoyer systematiquement un FCM high-priority
- L'utilisateur reveille par FCM demarre son foreground service, envoie un heartbeat, et devient "online"
- Si le FCM n'arrive pas (pas de token, token expire) : l'escalade continue au suivant au prochain cycle (15 min)

La detection on-call 15min (position 1 offline → alarme) reste inchangee : basee uniquement sur le heartbeat du user d'astreinte.

### Gestion du mode dans l'app Android

- Nouveau champ dans `User` model : `is_oncall: Boolean` (ou derive de la position 1 dans la chaine d'escalade)
- Au login, le backend renvoie l'info `is_oncall` dans le `TokenResponse` (ou via un endpoint dedie)
- Si `is_oncall == true` : demarrage immediat du foreground service (comportement actuel)
- Si `is_oncall == false` : PAS de foreground service, juste enregistrement du token FCM puis l'app peut etre fermee/mise en arriere-plan
- Un admin peut changer l'astreinte (ex: rotation), ce qui envoie un event SSE/FCM aux concernes pour qu'ils basculent de mode

## Objectif

Implementer 2 features complementaires en TDD (RED puis GREEN) :

### Feature 1 — SSE (Server-Sent Events)

**Backend :**
- Creer `backend/app/sse.py` : bus d'evenements interne avec `asyncio.Queue` par client connecte
  - `EventBus` singleton : `subscribe() -> queue`, `unsubscribe(queue)`, `publish(event_type, data)`
  - Ping periodique toutes les 15s (commentaire SSE `: ping\n\n`) pour detecter les deconnexions
- Creer endpoint `GET /api/events/stream` dans un nouveau fichier `backend/app/api/events.py`
  - Requiert `get_current_user` (auth Bearer)
  - Retourne `StreamingResponse` avec `media_type="text/event-stream"`
  - Format : `event: <type>\ndata: <json>\n\n`
  - Types d'evenements : `alarm_created`, `alarm_updated`, `alarm_resolved`, `alarm_escalated`, `user_status_changed`
- Brancher `EventBus.publish()` dans les mutations existantes :
  - `api/alarms.py` : send_alarm, acknowledge, resolve, reset
  - `escalation.py` : escalation_loop (ack expiry reactivation, escalation, on-call alarm)
  - `api/devices.py` : heartbeat (changement online/offline)
  - `watchdog.py` : passage offline
- Enregistrer le router events dans `main.py`

**Android (mode astreinte uniquement) :**
- Ajouter un client SSE dans `AlarmPollingService.kt` :
  - Connexion `OkHttp` (deja en dependance) vers `/api/events/stream` avec header `Accept: text/event-stream`
  - Parser les evenements SSE et mettre a jour `currentAlarm`, `activeAlarmCount`
  - Reconnexion automatique avec backoff exponentiel (1s, 2s, 4s, max 30s)
  - IMPORTANT : le heartbeat POST reste a 3s, inchange. C'est la seule source de verite pour le statut online/offline et la detection on-call 15min. Ne pas le modifier.
  - SSE remplace UNIQUEMENT le polling `GET /alarms/mine`. Le polling devient un fallback : ne se declenche que si SSE deconnecte depuis > 5s
- Mettre a jour `ApiService.kt` : pas de nouvelle route Retrofit (SSE utilise OkHttp directement)

### Feature 2 — FCM (Firebase Cloud Messaging)

**Backend :**
- Creer `backend/app/fcm_service.py` (meme pattern que `email_service.py`) :
  - `_last_fcm` variable module-level pour les tests (stocker une LISTE de tous les FCM envoyes, pas juste le dernier)
  - `send_fcm_notification(user_id, token, title, body, data)` : ajoute a `_last_fcm_list` + POST HTTP vers `https://fcm.googleapis.com/v1/projects/{PROJECT_ID}/messages:send` si `FCM_SERVICE_ACCOUNT_JSON` est configure
  - `get_last_fcm_list()` / `reset_last_fcm()` pour les tests
  - `send_fcm_to_user(db, user_id, data)` : helper qui cherche tous les tokens du user et envoie a chacun
  - `FCM_PROJECT_ID` et `FCM_SERVICE_ACCOUNT_JSON` en variables d'environnement
  - Priority "high" pour toutes les alarmes
- Creer table `device_tokens` dans `models.py` :
  - `id`, `user_id` (FK users), `fcm_token` (unique), `device_id` (unique par user), `created_at`, `updated_at`
- Ajouter endpoints dans `api/devices.py` :
  - `POST /api/devices/fcm-token` : enregistrer/mettre a jour un token FCM (body: `{token, device_id}`)
  - `DELETE /api/devices/fcm-token` : supprimer un token (au logout)
  - Les deux requierent `get_current_user`
- Brancher l'envoi FCM dans les mutations d'alarme :
  - `api/alarms.py` send_alarm : envoyer FCM a l'utilisateur assigne
  - `escalation.py` : envoyer FCM au nouvel utilisateur lors de l'escalade + a tous les users deja notifies
  - L'envoi FCM est fire-and-forget (ne bloque pas le flux principal)
- Modifier `_find_next_online_user()` → `_find_next_user()` : ne plus filtrer sur `is_online`
- Endpoint test : `GET /api/test/last-fcm` dans `test_api.py`
- Migration DB dans `database.py` (`_migrate_device_tokens`)

**Android :**
- Ajouter les dependances Firebase dans `build.gradle.kts` :
  - `implementation(platform("com.google.firebase:firebase-bom:33.0.0"))`
  - `implementation("com.google.firebase:firebase-messaging")`
  - Plugin `com.google.gms.google-services`
- Creer `AlarmWakeUpHandler.kt` dans `service/` :
  - Classe testable qui recoit les donnees du push et demarre le foreground service
  - `fun onAlarmPushReceived(context, alarmId, title, severity)` : lance `AlarmPollingService` avec les infos
- Creer `AlarmFirebaseService.kt` dans `service/` :
  - Extend `FirebaseMessagingService`
  - `onMessageReceived` : extrait les data, delegue a `AlarmWakeUpHandler`
  - `onNewToken` : envoie le nouveau token au backend via `POST /api/devices/fcm-token`
- Modifier `MainActivity.kt` :
  - Au login : recuperer le token FCM et l'envoyer au backend
  - Verifier `is_oncall` dans la reponse login :
    - Si `true` → demarrer le foreground service (comportement actuel)
    - Si `false` → enregistrer le token FCM uniquement, pas de foreground service
- Creer un mode "reveil FCM" dans `AlarmPollingService.kt` :
  - Quand demarre par `AlarmWakeUpHandler` (via FCM), le service s'active temporairement
  - Quand l'alarme est resolue (detectee via SSE ou polling), le service s'arrete automatiquement si `is_oncall == false`
- Mettre a jour `FakeApiService.kt` : ajouter les mocks pour `fcm-token`
- Mettre a jour `ApiService.kt` : ajouter les routes `fcm-token`

## Tests RED a ecrire

### Backend (`tests/test_sse.py` — nouveau fichier)
1. `test_sse_connection_requires_auth` : GET /api/events/stream sans token → 401/403
2. `test_sse_receives_alarm_created` : connecter SSE, envoyer alarme, verifier event recu
3. `test_sse_receives_alarm_acknowledged` : connecter SSE, ack alarme, verifier event
4. `test_sse_receives_alarm_resolved` : connecter SSE, resolve, verifier event
5. `test_sse_receives_escalation_event` : connecter SSE, trigger escalation via `/test/trigger-escalation`, verifier event
6. `test_sse_ping_keepalive` : connecter SSE, attendre ~16s, verifier qu'un ping est recu
7. `test_sse_multiple_clients` : 2 clients SSE, envoyer alarme, les 2 recoivent l'event
8. `test_sse_content_type` : verifier `Content-Type: text/event-stream`

### Backend (`tests/test_fcm.py` — nouveau fichier)
1. `test_register_fcm_token` : POST /api/devices/fcm-token → 200, token stocke
2. `test_register_fcm_token_requires_auth` : sans token → 401
3. `test_register_fcm_token_update` : re-enregistrer meme device_id → met a jour le token
4. `test_delete_fcm_token` : DELETE /api/devices/fcm-token → 200, token supprime
5. `test_alarm_sends_fcm` : envoyer alarme, verifier /test/last-fcm contient le bon payload pour l'utilisateur assigne
6. `test_escalation_sends_fcm` : trigger escalation, verifier FCM envoye au nouvel utilisateur
7. `test_fcm_sent_to_all_notified_users` : verifier que tous les users notifies recoivent un push
8. `test_fcm_no_token_no_crash` : envoyer alarme a un user sans token FCM → pas d'erreur, juste skip
9. `test_escalation_ignores_online_status` : user2 est offline, trigger escalation → l'alarme est quand meme assignee a user2 (+ FCM envoye)

### Backend (`tests/test_user_modes.py` — nouveau fichier)
1. `test_login_returns_is_oncall` : login user1 (position 1) → `is_oncall: true` dans la reponse
2. `test_login_returns_not_oncall` : login user2 (position 2) → `is_oncall: false`
3. `test_oncall_changes_when_escalation_config_updated` : modifier la chaine d'escalade, verifier que `is_oncall` change

### Android (`AlarmE2ETest.kt` — tests supplementaires)
1. `test_fcm_token_sent_on_login` : apres login, verifier que POST /api/devices/fcm-token est appele
2. `test_wake_up_handler_starts_service` : appeler `AlarmWakeUpHandler.onAlarmPushReceived()` avec un payload, verifier que le foreground service demarre
3. `test_sse_fallback_to_polling` : simuler SSE deconnecte, verifier que le polling reprend en < 5s
4. `test_non_oncall_user_no_foreground_service` : login avec `is_oncall=false`, verifier que le foreground service n'est PAS demarre
5. `test_non_oncall_user_fcm_starts_service` : simuler reception FCM via `AlarmWakeUpHandler`, verifier que le service demarre meme en mode veille

### Smoke test script (`tests/test_fcm_smoke.sh`)
Script shell pour test d'integration reelle (hors CI rapide) :
```
1. adb install app
2. adb reverse tcp:8000 tcp:8000
3. Lancer l'app, login en user2 (non-astreinte)
4. Verifier que le foreground service ne tourne PAS
5. curl POST /api/test/trigger-escalation (escalader jusqu'a user2)
6. sleep 10
7. adb shell dumpsys activity services | grep AlarmPollingService → verifier running
8. curl POST /api/alarms/{id}/resolve
9. sleep 5
10. adb shell dumpsys activity services | grep AlarmPollingService → verifier stopped
```

## Separation des responsabilites

Chaque couche a UN SEUL role. Ne pas les melanger :

| Couche | Role | Qui | Frequence | Ce qu'elle NE fait PAS |
|--------|------|-----|-----------|----------------------|
| **Heartbeat POST** | Preuve de vie pour watchdog + detection on-call 15min | Mode astreinte uniquement | 3s (inchange) | Ne transporte pas de donnees alarme |
| **SSE** | Push temps reel des evenements (alarmes, escalades, resolutions) | Mode astreinte + mode veille actif (apres reveil FCM) | Connexion permanente | N'est PAS un indicateur de presence |
| **FCM** | Reveil de l'app tuee + notification aux users en mode veille | Tous les users (cout zero au repos) | A la demande | N'est PAS un keepalive |

La detection on-call (user offline > 15min → alarme suivant) repose UNIQUEMENT sur le heartbeat POST + watchdog du user d'astreinte (position 1). Le SSE et le FCM n'interviennent pas dans cette logique.

## Contraintes techniques

- **TDD** : ecrire TOUS les tests RED d'abord, attendre validation, puis GREEN
- **Pas de Thread.sleep** dans les tests backend : utiliser l'horloge injectable
- **SSE tests** : utiliser `sseclient-py` ou parser manuellement le stream avec `requests` en mode stream
- **Tests Espresso** : utiliser `FakeApiService`, pas de vrai backend
- **IdlingResources** preferes a `Thread.sleep()` cote Espresso
- **Pas de MockK** cote Android (problemes Dalvik/ART) → `FakeApiService` / `AlarmWakeUpHandler` testable
- **Docker** : ajouter `FCM_PROJECT_ID` et `FCM_SERVICE_ACCOUNT_JSON` optionnels dans docker-compose.yml
- **Migration DB** idempotente pour `device_tokens` (meme pattern que `_migrate_alarm_notifications`)
- **Retrocompat** : le polling 3s doit continuer a fonctionner si SSE n'est pas disponible (fallback)
- **google-services.json** : ajouter au `.gitignore`, creer un `google-services.json.example` avec la structure attendue
- **Batterie** : les users en mode veille ne doivent avoir AUCUN service/job actif au repos. Seul FCM (via Google Play Services) tourne.
- **Escalade** : ne plus filtrer sur `is_online`. L'ordre de la chaine est respecte. Le FCM reveille les users en mode veille.

## Ordre d'implementation recommande

1. Tests RED backend SSE (`test_sse.py`)
2. Tests RED backend FCM (`test_fcm.py`)
3. Tests RED backend modes (`test_user_modes.py`)
4. Tests RED Android (dans `AlarmE2ETest.kt`)
5. → PAUSE : validation utilisateur des tests
6. GREEN backend : `sse.py`, `api/events.py`, branchement mutations
7. GREEN backend : `fcm_service.py`, models, migration, endpoints, `_find_next_user()`
8. GREEN backend : `is_oncall` dans login response
9. GREEN Android SSE : client SSE dans `AlarmPollingService.kt`
10. GREEN Android FCM : `AlarmFirebaseService.kt`, `AlarmWakeUpHandler.kt`
11. GREEN Android modes : logique astreinte/veille dans `MainActivity.kt` + arret service post-resolution
12. Smoke test script
13. Run all tests (backend + Android)
