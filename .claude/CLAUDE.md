# Contexte projet — Alarm 2.0

## Process TDD établi avec l'utilisateur

### Convention RED → GREEN
1. **RED** : Écrire les tests d'abord (pytest côté backend, Espresso côté Android)
2. L'utilisateur valide les tests
3. **GREEN** : Implémenter le code pour faire passer les tests
4. Ne JAMAIS modifier les tests pour les faire passer — modifier le code de production

### Règles respectées
- **Tests E2E uniquement** — aucun test unitaire (choix explicite de l'utilisateur)
- **Mocks autorisés** si pertinent (ex: FakeApiService côté Android)
- **Backend** : tests Python avec `requests` contre un backend live (Docker Compose)
- **Android** : tests Espresso isolés via `FakeApiService` (aucun backend nécessaire)
- **IdlingResources** préférés à `Thread.sleep()` côté Espresso
- **Horloge injectable** côté backend pour tester les délais (pas de sleep dans les tests)
  - `POST /test/advance-clock?minutes=16` pour avancer le temps
  - `POST /test/reset-clock` pour réinitialiser
  - Utilisée pour tester escalade, expiration d'ack, watchdog
- **Mailhog** pour tester l'envoi d'email SMTP réel sans envoyer de vrais emails

### Anti-patterns à éviter
- Pas de `Thread.sleep(15 * 60 * 1000)` pour tester des délais → horloge injectable
- Pas de MockK sur Android (problèmes avec Dalvik/ART) → utiliser `FakeApiService`
- Pas de coordonnées hardcodées pour les taps UI → utiliser Espresso `onView(withId(...))`
- Ne pas supprimer la DB pour changer le schéma → migration
- Ne pas lancer 2 émulateurs pendant les tests (interférences heartbeat)

### Structure des tests
- `tests/test_e2e.py` : 50 tests backend (pytest)
- `android/app/src/androidTest/java/com/alarm/critical/AlarmE2ETest.kt` : 18 tests Espresso
- `android/app/src/androidTest/java/com/alarm/critical/FakeApiService.kt` : Fake API
- `android/app/src/androidTest/java/com/alarm/critical/PollingIdlingResource.kt` : IdlingResource

## Architecture DI (Dependency Injection)
- `ApiProvider` : singleton holder, `override(mock)` / `reset()`
- Production : `ApiProvider.service = ApiClient.service` (Retrofit)
- Tests : `ApiProvider.override(FakeApiService())`

## Décisions techniques
- **SQLite → PostgreSQL** migration faite (Docker volume `pgdata`)
- **Email** : champ supprimé, login par nom d'utilisateur uniquement
- **Noms** : lowercase, sans espaces, case-insensitive au login
- **Une seule alarme active** à la fois (HTTP 409 si doublon)
- **Rotation bloquée** en portrait sur l'app mobile
- **Sonnerie continue** pour : alarme active, perte heartbeat > 2min, échec refresh token
- **Escalade cumulative** : tous les utilisateurs appelés continuent de sonner, pas seulement le dernier

## Comptes de test
- admin / admin123 (admin, escalade position 3)
- user1 / user123 (escalade position 1)
- user2 / user123 (escalade position 2)

## Commandes courantes
```bash
# Lancer le backend
docker compose up --build -d

# Tests backend
python -m pytest tests/test_e2e.py -v

# Tests Android
cd android && ./gradlew connectedAndroidTest

# Installer l'app sur émulateur
adb install -r android/app/build/outputs/apk/debug/app-debug.apk
adb reverse tcp:8000 tcp:8000
```
