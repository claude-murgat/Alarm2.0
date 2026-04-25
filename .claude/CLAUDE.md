# Contexte projet — Alarme Murgat

## ⚠️ LECTURE OBLIGATOIRE EN DÉBUT DE SESSION

Avant toute modification du code ou écriture de tests, **consulter systématiquement** :

- **[tests/INVARIANTS.md](../tests/INVARIANTS.md)** — Catalogue des invariants métier (source de vérité).
  Chaque règle business est listée avec ID stable (INV-XXX), criticité, statut (✅/⚠️/❌/🐛).
  C'est le document qui dicte ce que les tests doivent vérifier et ce que le code doit garantir.
  Ne PAS lire le code source pour déduire le comportement attendu — lire le catalogue.
  Si un invariant manque ou est ambigu, demander au propriétaire AVANT d'implémenter/tester.

- **[tests/audit_v2.json](../tests/audit_v2.json)** — Audit critique indépendant de la suite de tests
  (bugs réels trouvés, blind spots, architecture recommandée).

- **[android/INVARIANTS.md](../android/INVARIANTS.md)** — Invariants côté app Android (à créer).

Règle d'or : **les tests viennent de la spec (catalogue), pas du code**. Un bug dans le code
ne doit jamais être figé par un test — le catalogue tranche.

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
- `tests/test_e2e.py` : 119 tests backend (pytest) — principal
- `tests/test_fcm.py` : 11 tests FCM
- `tests/test_improvements.py` : 28 tests améliorations
- `tests/test_user_modes.py` : 5 tests modes astreinte/veille + escalation_position
- `tests/test_frontend.py` : 18 tests interface web (inclut TestUsersTab)
- `tests/test_failback.py` : tests failback (cluster)
- **Total backend : ~181 tests**
- `android/app/src/androidTest/java/com/alarm/critical/AlarmE2ETest.kt` : 22 tests Espresso
- **Total : ~203 tests (181 backend + 22 Android)**

## Architecture

### DI (Dependency Injection)
- `ApiProvider` : singleton holder, `override(mock)` / `reset()`
- Production : `ApiProvider.service = ApiClient.service` (Retrofit)
- Tests : `ApiProvider.override(FakeApiService())`

### Infrastructure (cluster 3 noeuds)
- **Patroni + PostgreSQL** : replication HA (1 leader + 2 replicas)
- **etcd** : consensus distribue pour Patroni (3 instances)
- **Backends** : 3 instances FastAPI (ports 8000, 8001, 8002)
- **Failover Android** : rotation circulaire des URLs backend (ApiClient)
- **Fichiers env** : `.env.node1`, `.env.node2`, `.env.node3`, `.env.dev` (single-node)

### Android — Ecrans
- **MainActivity** : login (pre-rempli en debug)
- **DashboardActivity** : dashboard principal avec Navigation Drawer, carte hero alarme, ArcTimerView, badge escalade/garde, bouton aide flottant (export logs)
- **AlarmActivity** : ecran plein ecran alarme avec pulsation, transition vert post-ack, ArcTimerView

### Android — Composants customs
- **ArcTimerView** (`view/ArcTimerView.kt`) : arc de cercle animé pour timer acquittement
- **AppLogger** (`util/AppLogger.kt`) : collecte les 500 derniers events, export via share intent pour debug par messagerie

### Backend — Endpoints notables
- `GET /api/stats/kpi?weeks=8&hors_heures_only=true` : KPIs astreinte (alarmes/semaine, taux escalade, MTTR, top recurrentes)
- `GET /api/alarms/?days=10` : historique alarmes limite a N jours (defaut 10, max 90)
- `POST /api/config/escalation/bulk` : modifie la chaine + envoie push FCM a tous les utilisateurs
- `GET /api/config/escalation` : chaine d'escalade (public, pas d'auth)

### Frontend web
- Single-file HTML (`backend/app/templates/index.html`) avec Chart.js
- Onglets : Tableau de bord, Escalade, Statistiques, Utilisateurs, Alarmes, Tests, Log, Cluster
- Stats : graphe barres alarmes/semaine, camembert escalade, top recurrentes, filtre hors heures France

## Décisions techniques
- **Nom de l'app** : "Alarme Murgat" (renomme depuis "Alarme Critique")
- **Gravite supprimee** : toujours "critical", pas de selection utilisateur
- **Assignation supprimee** : toujours auto-escalade, pas de selection manuelle
- **SQLite → PostgreSQL** migration faite (Docker volume `pgdata`)
- **Login par nom uniquement** : lowercase, sans espaces, case-insensitive
- **Une seule alarme active** a la fois (HTTP 409 si doublon)
- **Rotation bloquee** en portrait sur l'app mobile
- **Sonnerie continue** pour : alarme active, perte heartbeat > 2min, echec refresh token
- **Escalade cumulative** : tous les utilisateurs appeles continuent de sonner
- **Logout supprime le token FCM** cote backend (plus de push apres deconnexion)
- **Push FCM sur changement chaine** : notifie tous les utilisateurs de leur nouvelle position
- **Statut "En attente"** pour les non-astreinte (pas de heartbeat actif, reveille par push)
- **Hors heures France** : filtre stats — exclut 8h-12h / 14h-17h semaine, inclut WE + feries

## Comptes de test
- admin / admin123 (admin, escalade position 3)
- user1 / user123 (escalade position 1, de garde)
- user2 / user123 (escalade position 2)

## Commandes courantes
```bash
# Lancer le cluster complet (3 noeuds)
# 1. D'abord les 3 etcd
COMPOSE_PROFILES=mailhog docker compose --env-file .env.node1 -p node1 up -d etcd
docker compose --env-file .env.node2 -p node2 up -d etcd
docker compose --env-file .env.node3 -p node3 up -d etcd
# 2. Attendre healthy puis lancer le reste
COMPOSE_PROFILES=mailhog docker compose --env-file .env.node1 -p node1 up --build -d
docker compose --env-file .env.node2 -p node2 up --build -d
docker compose --env-file .env.node3 -p node3 up --build -d

# Lancer en mode dev single-node
COMPOSE_PROFILES=mailhog docker compose --env-file .env.dev -p dev up --build -d

# Tests backend
python -m pytest tests/test_e2e.py -v
python -m pytest tests/test_user_modes.py -v
python -m pytest tests/test_frontend.py -v  # necessite playwright

# Regenerer .test_durations pour pytest-split (tier 3 load-balancing)
# A faire apres ajout/refactor de tests substantiels (cf audit CI parallelisation).
# Sans ce fichier, asymetrie worker1=7min vs worker2=14min sur tier 3.
# Prerequis : cluster up (docker compose --env-file .env.dev -p dev up -d).
./scripts/regenerate-test-durations.sh
# puis : git add .test_durations && commit + PR

# Mutation testing local (logique pure backend/app/logic/, ~3 min sur ce poste)
# Permet de boucler sans attendre le nightly. Workflow CI nightly = identique.
# Windows : PYTHONIOENCODING=utf-8 obligatoire (mutmut crashe sur l'emoji 🎉
# affiche pour chaque kill car cp1252 ne sait pas l'encoder).
PYTHONIOENCODING=utf-8 python -m mutmut run \
  --paths-to-mutate backend/app/logic/ \
  --tests-dir tests/unit/ \
  --runner "python -m pytest -x --assert=plain -m unit -p no:randomly tests/unit/"
PYTHONIOENCODING=utf-8 python -m mutmut results       # liste survivants par module
PYTHONIOENCODING=utf-8 python -m mutmut html          # rapport navigable (./html/)
PYTHONIOENCODING=utf-8 python -m mutmut show <id>     # diff d'un mutant precis
PYTHONIOENCODING=utf-8 python -m mutmut junitxml      # XML pour scoring (cf .github/scripts/mutation_score.py)

# Build + install Android sur emulateur
cd android && ./gradlew assembleDebug
adb install -r android/app/build/outputs/apk/debug/app-debug.apk
adb shell pm clear com.alarm.critical  # reset donnees
adb shell am start -n com.alarm.critical/.MainActivity

# Emulateur avec reseau fonctionnel
# Utiliser AVD "alarm_explore" (alarm_test a le reseau casse)
# URLs emulateur : 10.0.2.2 (build.gradle.kts)
# URLs telephone physique : changer en IP reseau (ex: 172.16.2.191)
```

## Bot IA contributeur (alarm-murgat-bot)

Le projet dispose d'un bot IA qui transforme des bug reports GitHub en PRs
(test RED + fix GREEN) via Claude Code CLI headless en CI.

- **Identité** : GitHub App `alarm-murgat-bot[bot]` (App ID 3428066, installation 125174785)
- **Modèle** : Opus 4.7 (OAuth Max via `CLAUDE_CODE_OAUTH_TOKEN` en secret GH)
- **Workflow** : `.github/workflows/ai-bot.yml`
- **System prompt** : `.github/ai-bot/prompt.md` (règles P1-P6 du projet)
- **Déclencher aujourd'hui (phase 2)** : `gh workflow run ai-bot.yml -f issue_number=<N>`
- **Déclencher phase 3 (à venir)** : poser label `ai:fix` sur une issue
- **Doc utilisateur** : `.github/ai-bot/README.md`
- **Stratégie & design** : `docs/AI_STRATEGY.md`

Règles appliquées par le bot :
- Tests viennent de **INVARIANTS.md** (spec), jamais du code (P1)
- TDD strict : test RED prouvé avant le fix, sinon abandon (P5)
- Budget 5 tests par fix maximum (P4)
- Denylist dure : `.github/workflows/**`, `infra/**`, `tests/INVARIANTS.md`,
  `tests/conftest.py`, `docs/AI_STRATEGY.md`, `.claude/**`, etc.
- Review humaine obligatoire tant que le bot n'a pas 10 PRs propres consécutives

## Améliorations futures
- **Agent autonome de debug Android** : déployer un agent IA capable de recevoir les logs exportés par l'app (via bouton aide) et d'effectuer un diagnostic de premier niveau en autonomie. Distinct du bot IA contributeur (ci-dessus) — ce dernier fixe des bugs GH, pas des logs runtime Android.
- **Remote logging Firebase Crashlytics** : tracer les erreurs client sans intervention utilisateur
- **Push silencieux** pour changements de config (en complement du push visible actuel)
- **RecyclerView** pour l'historique si le volume d'alarmes depasse 200+ items
- **Tests Espresso** pour les nouveaux ecrans (Navigation Drawer, ArcTimerView, bouton aide)
