# Backlog IA — invariants à traiter par `alarm-murgat-bot`

> Source : `tests/INVARIANTS.md`. Stratégie : `docs/AI_STRATEGY.md`.
> Workflow bot : `.github/workflows/ai-bot.yml`. Denylist : `.github/ai-bot/denylist.txt`.
> Cron de pioche : `0 */4 * * *` sur la plus ancienne issue `ai:queue` (opérationnel depuis 2026-05-12).

## État au 2026-05-13

Issues créées originellement : 18 (#68-#85). Avancement :

| Statut | Compteur | Détail |
|---|---|---|
| ✅ **Mergées** | 6 | INV-082 (#68→PR #89), INV-007 (#69→PR #90), INV-110 (#70→PR #91), INV-074 (#71→PR #92), INV-077 (#73→PR #93), INV-078 (#72→PR #95) |
| ⏳ **`ai:queue` en attente** | 8 | #74 (INV-084 watchdog_timeout), #75 (INV-084 escalation_tick+watchdog_tick), #76-#78 (INV-018/018b décomposé), #79-#81 (INV-085 décomposé) |
| 🔧 **`human-required`** | 4 | #82 (INV-076 workflow CI), #83 (INV-093 split-brain), #84 (INV-095 atomicité failover), #85 (INV-018b frontend) |
| 🆕 **Issues follow-up** | 2 | #96 (INV-074 path négatif [H]) ai:queue, #97 (INV-074 stateless JWT [human-required]) |

Le cron pioche désormais automatiquement toutes les 4h. Pour accélérer : `gh workflow run ai-bot-cron.yml`.

## Comment ce fichier est utilisé

Chaque section décrit **une issue GitHub** prête à créer. Format de chaque issue :

- **Titre** : `[INV-XXX] <description>` (recopier tel quel)
- **Labels** : `ai:queue` (pile A1/A2) ou `human-required` (pile A3)
- **Body** : à passer via `--body-file -` ou `-F body.md`

Pour créer toutes les issues d'une pile :

```bash
# Exemple pile A1 issue 1 (cf bloc plus bas)
gh issue create \
  --title "[INV-082] Test concurrence /config/escalation/bulk (race read/write)" \
  --label ai:queue \
  --body-file - <<'BODY'
...corps ci-dessous...
BODY
```

## Pile A1 — atomiques bot (1 issue, ≤ 5 tests, code hors denylist)

### A1.1 — `[INV-082] Test concurrence /config/escalation/bulk (race read/write)`

**Labels** : `ai:queue`

**Body** :

```markdown
## Invariant
INV-082 (H) — `POST /api/config/escalation/bulk` est atomique : DELETE + INSERTs
dans une transaction unique. Un `GET /api/config/escalation` concurrent ne doit
JAMAIS observer 0 ligne tant que la chaîne précédente était non-vide.

## Contexte
L'audit 2026-04-20 confirme que le code est déjà atomique :
- `SessionLocal` configurée `autocommit=False, autoflush=False`
  (`backend/app/database.py:15`)
- `save_escalation_chain_bulk` (`backend/app/api/config.py:120-144`) :
  DELETE → INSERTs → un seul `db.commit()`
- PostgreSQL READ COMMITTED garantit l'isolation.

**Le code n'a donc PAS besoin d'être modifié.** L'objectif est d'ajouter un test
de race qui **verrouille** cette propriété en régression — si quelqu'un casse
l'atomicité (ex: split en 2 commits, ouverture d'une nouvelle session entre
DELETE et INSERT), le test doit échouer.

## Repro / scénario test
Thread A : boucle 50× `POST /api/config/escalation/bulk` avec 3 users.
Thread B : boucle 200× `GET /api/config/escalation` en parallèle.

**Assertion** : B ne lit JAMAIS une réponse vide (toujours ≥ 1 entrée).

## Zone autorisée
- Tests : `tests/integration/test_escalation_config_atomicity.py` (nouveau, tier 2)
  OU `tests/test_e2e.py` (tier 3 si SQLite TestClient ne suffit pas — préférer
  tier 2)
- Production code : **rien à modifier**. Si vous trouvez un cas où le test fail,
  c'est un bug à reporter, pas à corriger silencieusement.

## Hors-scope
- Toucher `backend/app/api/config.py`
- Toucher la configuration SQLAlchemy

## Critères de succès
- 1 nouveau test, marker `@pytest.mark.integration` ou tier 3 selon faisabilité
- Tier 1 toujours vert
- Test FAIL si on commente le `db.commit()` final (preuve qu'il prouve quelque chose)
- Budget P4 : 1 test (max 2 si une variante avec chaîne vide initiale est utile)
```

---

### A1.2 — `[INV-084] Migrer WATCHDOG_TIMEOUT_SECONDS hardcoded vers SystemConfig`

**Labels** : `ai:queue`

**Body** :

```markdown
## Invariant
INV-084 (C, sous-cas 1/2 restant) — Aucun délai métier ne doit être hardcodé.
`watchdog_timeout_seconds` est seedé dans `SystemConfig` mais le code lit la
constante Python `WATCHDOG_TIMEOUT_SECONDS = 60` au lieu de la valeur DB.

Référence : `backend/app/watchdog.py:14` et `:31`.

Précédent comparable : PR #25 (sous-cas `oncall_offline_delay_minutes`) a
migré la même chose pour la clé oncall. Suivre le même pattern :
1. Renommer la constante en `WATCHDOG_TIMEOUT_SECONDS_DEFAULT` (fallback)
2. Lire `SystemConfig.watchdog_timeout_seconds` à chaque tick (pas de cache)
3. Si la clé est absente en DB → utiliser le default

## Repro / pourquoi
Aujourd'hui, un admin qui modifie `watchdog_timeout_seconds` via l'UI/API
voit la valeur en DB changer mais le watchdog continue d'utiliser 60s.
Test possible : POST /api/config (modifier la valeur à 30s), attendre 1 tick,
créer un user avec `last_heartbeat = now - 35s`, vérifier qu'il devient offline.

## Zone autorisée
- `backend/app/watchdog.py` (lecture SystemConfig à chaque tick)
- `backend/app/main.py` (seed si pas déjà là — vérifier d'abord)
- Tests : `tests/integration/` ou `tests/unit/` selon ce qui est testable purement

## Hors-scope
- Toucher `escalation_tick_seconds` (autre issue A1.3)
- Refactor du watchdog au-delà de la lecture de la config

## Critères de succès
- Constante renommée en `*_DEFAULT`, lecture DB à chaque tick
- 1 test unit (logic pure si extractable) OU 1 test intégration vérifiant
  qu'une modif de SystemConfig change le seuil
- Budget P4 : 2 tests max
- Pas de cache de la valeur (lecture à chaque tick — la flexibilité prime)
```

---

### A1.3 — `[INV-084] Migrer escalation_tick_seconds + watchdog_tick_seconds vers SystemConfig (2 clés séparées)`

**Labels** : `ai:queue`

**Body** :

```markdown
## Invariant
INV-084 (C, sous-cas 2/2 restant) — `escalation_tick_seconds` (10s) est seedé
dans `SystemConfig` mais le code utilise une valeur hardcodée pour la période
de la boucle d'escalade. Idem `watchdog.py` qui `asyncio.sleep(30)` en dur.

Référence catalogue : `backend/app/escalation.py:331` (tick hardcodé) et
`backend/app/watchdog.py:48` (sleep 30s hardcodé).

## Décision actée — 2 clés séparées
Le catalogue suggérait "probable partagée, simplifie". **Décision projet
2026-05-12 : 2 clés séparées**, parce que :
- Sémantique propre : "fréquence évaluation escalade" ≠ "fréquence check offline"
- 2 leviers indépendants pour accélérer les tests
- Coût marginal négligeable (+1 lecture SystemConfig, +1 seed)

À implémenter :
- `escalation_tick_seconds` (default 10) → lue par `escalation.py`
- `watchdog_tick_seconds` (default 30) → lue par `watchdog.py`
- Seeder les 2 clés dans `main.py` si absentes.

## Pourquoi
- Flexibilité admin (peu utile en prod, où 10s/30s sont OK)
- **Surtout** : accélération tests. `escalation_tick_seconds=1` réduit
  une boucle d'attente de 10s à 1s par tick — gain massif sur tier 3 (~7 min).

Précédent à suivre : PR #25 pour `oncall_offline_delay_minutes`.

## Zone autorisée
- `backend/app/escalation.py` (lecture `escalation_tick_seconds` au démarrage
  de la boucle ; recharge à chaque itération préférée — permet la modif live)
- `backend/app/watchdog.py` (lecture `watchdog_tick_seconds`, idem)
- `backend/app/main.py` (seed des 2 clés si absentes)
- Tests : tier 1 (logique pure si extractible) ou tier 2

## Hors-scope
- Toucher `watchdog_timeout_seconds` (autre issue A1.2)
- Modifier les durées par défaut (10s et 30s restent les défauts)
- Partager une seule clé entre les deux loops (décision tranchée ci-dessus)

## Critères de succès
- 2 constantes renommées en `*_DEFAULT`, lecture DB à chaque itération
- 2-3 tests vérifiant qu'une modif SystemConfig change la cadence observable
- Test du watchdog indépendant du test de l'escalade
- Budget P4 : 3 tests max
```

---

### A1.4 — `[INV-074] Test : POST /auth/refresh produit un nouveau token`

**Labels** : `ai:queue`

**Body** :

```markdown
## Invariant
INV-074 (C, ⚠️ partiellement couvert) — `POST /auth/refresh` avec un token
valide retourne un nouveau token, **différent** du token courant.

## Pourquoi
Un refresh qui retourne le même token ne sert à rien (le client peut
continuer à l'utiliser passivement, mais ne rafraîchit pas son TTL si la
logique de rotation n'est pas appliquée). Le test doit FAIL si le code se
contente de renvoyer le token reçu en input.

## Zone autorisée
- Tests : `tests/integration/test_auth_refresh.py` (nouveau, tier 2) ou
  `tests/test_e2e.py` (tier 3)
- Production : **lire d'abord le code de refresh**. Si le test passe vert tout
  de suite, l'invariant est déjà respecté → noter dans le catalogue (PR
  description suggère de passer ⚠️ → ✅). Si le test fail → fix minimal.

## Hors-scope
- Rotation de secret JWT
- Mécaniques de blacklist

## Critères de succès
- 2 tests :
  - `new_refresh_returns_distinct_token` : `token_after != token_before`
  - `new_token_is_usable` : le nouveau token authentifie un GET protégé
- Budget P4 : 2 tests
```

---

### A1.5 — `[INV-077] Test : endpoints admin-only retournent 403 pour non-admin`

**Labels** : `ai:queue`

**Body** :

```markdown
## Invariant
INV-077 (H, ⚠️ partiellement couvert) — Les endpoints suivants exigent
`is_admin=True` et retournent **403** sinon :
- `DELETE /api/users/{id}`
- `POST /api/alarms/reset`
- `POST /api/config/*` (au moins `/escalation`, `/escalation/bulk`, `/system`)

## Repro / pourquoi
Aujourd'hui un user non-admin pourrait potentiellement appeler ces endpoints.
Le test doit vérifier qu'un token user1 (non-admin) → 403, et qu'un token
admin → 200 (ou 4xx fonctionnel non-403).

## Zone autorisée
- Tests : `tests/integration/test_admin_only_endpoints.py` (nouveau, tier 2,
  préféré) ou tier 3
- Production : `backend/app/api/users.py`, `backend/app/api/alarms.py`,
  `backend/app/api/config.py` SI un endpoint n'est pas protégé

## Hors-scope
- Refactor de l'auth (continuer d'utiliser `get_current_user` existant)
- Renommer des endpoints

## Critères de succès
- 1 test paramétré sur 3-5 endpoints critiques
- Pour chacun : assert 403 si user1, assert non-403 si admin
- Si un endpoint est trouvé non-protégé → ajout du décorateur `Depends(get_current_admin)`
  ou équivalent (lire le code pour le nom exact)
- Budget P4 : 1 test paramétré (≤ 5 cases)
```

---

### A1.6 — `[INV-078] Test : logout supprime le token FCM côté backend`

**Labels** : `ai:queue`

**Body** :

```markdown
## Invariant
INV-078 (M, ⚠️ partiellement couvert) — `POST /api/devices/fcm-token`
avec méthode DELETE (ou `POST /api/auth/logout` selon l'API exacte) retire
le token FCM enregistré pour ce user. Conséquence : plus aucun push FCM ne
doit cibler ce token après logout.

CLAUDE.md confirme : "Logout supprime le token FCM côté backend (plus de
push après déconnexion)".

## Repro / scénario test
1. Login user1 → POST /devices/fcm-token avec token `t1`
2. Vérifier en DB que `t1` est enregistré
3. POST /auth/logout (ou DELETE /devices/fcm-token)
4. Vérifier en DB que `t1` n'est plus enregistré
5. Bonus : créer une alarme assignée à user1 → vérifier que FCM service n'a
   pas tenté de push vers `t1`

## Zone autorisée
- Tests : `tests/integration/test_fcm_logout.py` (nouveau, tier 2) ou tier 3
- Production : `backend/app/api/users.py` (logout) ou `backend/app/api/...`
  selon l'endpoint exact

## Hors-scope
- Modifier le flux FCM en lui-même
- Ajouter une notion de "session active"

## Critères de succès
- 1-2 tests : présence avant, absence après
- Si le code laisse le token en DB → fix minimal (DELETE sur la table FCM)
- Budget P4 : 2 tests
```

---

### A1.7 — `[INV-007] Test : alarme resolved n'apparaît plus dans /active ni /mine`

**Labels** : `ai:queue`

**Body** :

```markdown
## Invariant
INV-007 (M, ⚠️ partiellement couvert) — `GET /api/alarms/active` et
`GET /api/alarms/mine` excluent les alarmes `status = 'resolved'`.

## Repro / scénario test
1. POST /alarms/send (status devient `active`)
2. POST /alarms/{id}/resolve (status devient `resolved`)
3. GET /alarms/active → liste vide (ou ne contient PAS l'id)
4. GET /alarms/mine (auth user notifié) → idem

## Zone autorisée
- Tests : `tests/test_e2e.py` (probable, dépend des endpoints utilisés) ou
  `tests/integration/` si TestClient suffit
- Production : `backend/app/api/alarms.py` filtres SI le test fail

## Hors-scope
- Modifier l'UI mobile (Android)
- Changer le statut "resolved" en autre chose

## Critères de succès
- 2 tests :
  - `resolved_alarm_excluded_from_active`
  - `resolved_alarm_excluded_from_mine`
- Si fail → ajout filtre `status != 'resolved'` dans le code
- Budget P4 : 2 tests
```

---

### A1.8 — `[INV-110] Test : GET /alarms?days=N clamp à [1, 90]`

**Labels** : `ai:queue`

**Body** :

```markdown
## Invariant
INV-110 (L, ⚠️ partiellement couvert) — `GET /api/alarms/?days=N` clamp N
dans `[1, 90]`. Donc :
- `days=0` ou `days=-5` → traité comme `days=1` (ou 422 selon contrat — lire
  le code une fois pour décider, mais le catalogue dit "clampé", donc on
  privilégie le clamp silencieux pour rester rétrocompatible)
- `days=10000` → traité comme `days=90`
- `days=10` → comportement normal

## Repro / scénario test
Créer 1 alarme. Appeler l'endpoint avec différents N → vérifier que la
réponse est cohérente avec un N effectif clampé.

## Zone autorisée
- Tests : `tests/integration/test_alarms_days_clamp.py` (tier 2)
- Production : `backend/app/api/alarms.py` (endpoint `/alarms/`) SI le clamp
  n'est pas implémenté

## Hors-scope
- Ajouter de nouveaux query params
- Changer le défaut (rester sur 10)

## Critères de succès
- 1 test paramétré sur ≥ 3 valeurs (out-of-range bas, in-range, out-of-range haut)
- Si clamp absent → ajout `days = max(1, min(90, days))` dans le handler
- Budget P4 : 1 test paramétré
```

---

## Pile A2 — gros INV décomposés en sous-issues bot

### A2.1 — INV-018 + INV-018b (3 sous-issues)

**Vue d'ensemble** : ajouter `Alarm.original_created_at` immuable, l'utiliser
pour toutes les lectures historiques (filtres, ORDER BY, schemas, stats KPI).
**Hors-scope bot** : frontend `index.html` (templates est denyliste → cf A3.4).

---

#### A2.1.a — `[INV-018] Ajouter colonne Alarm.original_created_at + écriture à la création`

**Labels** : `ai:queue`

**Body** :

```markdown
## Invariant
INV-018 (C, 🐛) — Nouveau champ `Alarm.original_created_at` :
- Initialisé à `clock_now()` à la création de l'alarme
- **Jamais modifié** ensuite (immuable)
- `created_at` continue d'être le "timer d'escalade" (reset à chaque escalade)

## Pourquoi
Aujourd'hui `created_at` est doublement utilisé : (1) date originale de
l'événement, (2) timer pour l'escalade (reset à chaque palier). Les deux
usages entrent en conflit : une alarme escaladée 2h après création apparaît
comme "il y a 2min" dans l'historique.

## Scope de cette sous-issue
**Uniquement** :
1. Ajouter `original_created_at = Column(DateTime, nullable=False)` au modèle
   `Alarm` dans `backend/app/models.py`
2. Initialiser `original_created_at = clock_now()` dans **toutes** les voies
   de création :
   - `POST /api/alarms/send` (`backend/app/api/alarms.py`)
   - `POST /api/test/send-alarm` (`backend/app/api/test_api.py`)
   - création d'alarme oncall (`backend/app/logic/oncall.py` ou
     `backend/app/escalation.py` — chercher `Alarm(`)
3. **Migration au startup** (pas d'Alembic dans ce projet) : dans
   `backend/app/main.py` lifespan, après `Base.metadata.create_all`, exécuter
   `UPDATE alarms SET original_created_at = created_at WHERE original_created_at IS NULL`
   (idempotent, applicable aux rows existants).
4. **Ne PAS toucher** aux lectures (filtres, ORDER BY, schemas, stats,
   frontend) — c'est l'objet des sous-issues A2.1.b/c/A3.4.

## Zone autorisée
- `backend/app/models.py`
- `backend/app/api/alarms.py`, `test_api.py`
- `backend/app/logic/oncall.py`, `backend/app/escalation.py` (uniquement
  call sites `Alarm(`)
- `backend/app/main.py` (lifespan UPDATE)
- Tests : `tests/unit/` ou `tests/integration/`

## Hors-scope
- Modifier les endpoints de lecture (filtre /alarms, ORDER BY /active)
- Modifier `schemas.py` (AlarmResponse) — fait en A2.1.b
- Modifier les stats (`backend/app/api/stats.py`) — fait en A2.1.c
- Frontend `templates/index.html` (denyliste — A3.4)

## Critères de succès
- Nouveau champ présent dans le schéma DB après startup
- À la création d'une alarme :
  `alarm.original_created_at == alarm.created_at` (identiques au temps t0)
- Après escalade :
  `alarm.original_created_at != alarm.created_at` et `original_created_at`
  inchangé
- 2-3 tests :
  - `test_original_created_at_set_at_creation` (unit ou integration)
  - `test_original_created_at_immutable_after_escalation` (integration)
  - `test_original_created_at_uses_clock_now` (vérifie qu'un advance-clock
    avant create se reflète)
- Tier 1 toujours vert
- Budget P4 : 3 tests
```

---

#### A2.1.b — `[INV-018b] Lectures historiques /alarms et schemas → original_created_at`

**Labels** : `ai:queue`

**Dépend de** : A2.1.a mergée.

**Body** :

```markdown
## Invariant
INV-018b (C, 🐛) — Les lectures historiques d'alarme doivent utiliser
`original_created_at`, PAS `created_at` (qui est un timer interne).

## Scope de cette sous-issue
Migrer les call sites suivants (extraits du catalogue) :
- `backend/app/api/alarms.py:116-117` — filtre `/alarms/?days=N`
- `backend/app/api/alarms.py:131` — `ORDER BY` pour `/active`
- `backend/app/schemas.py:124` — champ exposé dans `AlarmResponse`
  (renvoyer `original_created_at` au lieu de `created_at`, ou les deux)

**Important** : ne PAS toucher ces call sites qui utilisent `created_at`
légitimement comme timer (à laisser tels quels) :
- `backend/app/escalation.py:185` (calcul `elapsed` pour décision d'escalade)
- `backend/app/escalation.py:132, 204` (reset après ack expiry et escalade)
- `backend/app/calls.py:96` (reset DTMF escalate)

**Décision contrat API** : le champ exposé dans `AlarmResponse` doit-il être
renommé ou ajouté en plus ? Recommandation : exposer **les deux** champs
(`created_at` ET `original_created_at`) pour ne pas casser les consumers
existants (Android, frontend). Le frontend basculera sur `original_created_at`
dans A3.4.

## Zone autorisée
- `backend/app/api/alarms.py` (filtres + ORDER BY)
- `backend/app/schemas.py` (`AlarmResponse`)
- Tests : `tests/integration/`

## Hors-scope
- Stats KPI (A2.1.c)
- Frontend (A3.4)
- Toucher les usages timer (escalation.py, calls.py)

## Critères de succès
- Test E2E/intégration : créer 1 alarme, l'escalader (reset `created_at`),
  vérifier que :
  - `/api/alarms/?days=1` renvoie l'alarme (basé sur `original_created_at`,
    pas `created_at` post-escalade qui pourrait sortir de la fenêtre)
  - `/api/alarms/active` ORDER BY respecte la date originale
  - `AlarmResponse` expose `original_created_at` correct
- Tier 1 vert, pas de régression
- Budget P4 : 3 tests
```

---

#### A2.1.c — `[INV-018b] Stats KPI : bucketing par semaine + MTTR sur original_created_at`

**Labels** : `ai:queue`

**Dépend de** : A2.1.a mergée.

**Body** :

```markdown
## Invariant
INV-018b (C, 🐛, scope stats) — Les calculs KPI dans
`backend/app/api/stats.py` doivent utiliser `original_created_at` pour :
- `stats.py:108` — bucketing par semaine
- `stats.py:120` — comptage
- `stats.py:140` — calcul MTTR (différence resolved_at - original_created_at,
  PAS - created_at)
- `stats.py:143` — top récurrentes (date dans la fenêtre)

## Pourquoi
Une alarme escaladée 2h après création compte aujourd'hui dans la mauvaise
semaine, et son MTTR est artificiellement raccourci (mesuré depuis le dernier
reset de timer, pas l'événement original).

## Zone autorisée
- `backend/app/api/stats.py` (les 4 call sites cités)
- Tests : `tests/integration/test_stats_uses_original_created_at.py` (tier 2)
  OU `tests/test_e2e.py`

## Hors-scope
- Endpoints `/alarms` (A2.1.b)
- Frontend (A3.4)
- Changer la sémantique des KPI au-delà du bucket/MTTR

## Critères de succès
- Test scénario : alarme créée à T0, escalade à T0+2h (qui aurait changé la
  semaine de bucketing), resolved à T0+3h
- Vérifier :
  - `weeks` bucket = semaine de T0 (pas de T0+2h)
  - MTTR = 3h (pas 1h)
- 2-3 tests
- Budget P4 : 3 tests
```

---

### A2.2 — INV-085 (3 sous-issues)

**Vue d'ensemble** : email direction technique sur perte de quorum cluster,
avec anti-spam (reminders 1h/3h/6h). Décomposable en : (a) détection,
(b) email initial, (c) anti-spam reminders.

---

#### A2.2.a — `[INV-085] Détection perte de quorum cluster (3 min seuil)`

**Labels** : `ai:queue`

**Body** :

```markdown
## Invariant
INV-085 (C, 🐛, sous-cas 1/3) — Le système doit détecter une perte de quorum
selon les conditions :
- `quorum.has_quorum == False` dans `/api/cluster` (moins de N/2+1 noeuds
  healthy), OU
- Patroni injoignable depuis tous les noeuds pendant > **3 minutes**

Cette sous-issue traite UNIQUEMENT la détection. L'envoi d'email et
l'anti-spam sont dans A2.2.b et A2.2.c.

## Scope de cette sous-issue
1. Ajouter une fonction pure `evaluate_quorum_loss(snapshot, history)
   -> QuorumState` dans `backend/app/logic/` (nouveau fichier
   `quorum_detection.py` recommandé)
2. La fonction prend :
   - état actuel du cluster (`has_quorum: bool`, `patroni_reachable: bool`,
     `timestamp: datetime`)
   - historique récent (≥ 3 min)
   et retourne : `QuorumState(is_lost: bool, lost_since: datetime | None)`
3. Brancher cette fonction sur un tick périodique (probablement dans
   `escalation.py` ou un nouveau `quorum_watchdog.py` — choisir le moins
   intrusif).
4. **Persister** `lost_since` dans `SystemConfig` ou une nouvelle table
   `cluster_state` pour survivre aux restarts backend.

## Zone autorisée
- `backend/app/logic/quorum_detection.py` (nouveau)
- `backend/app/escalation.py` ou `backend/app/watchdog.py` (intégration tick)
- `backend/app/models.py` (si nouvelle table)
- `backend/app/main.py` (seed/init)
- Tests : `tests/unit/test_quorum_detection.py`

## Hors-scope
- Envoi email (A2.2.b)
- Reminders (A2.2.c)
- Modifier `/api/cluster` (lire la valeur existante suffit)

## Critères de succès
- Fonction pure testable sans cluster réel (entrées = snapshot, sortie =
  state)
- 3-4 tests unit :
  - quorum perdu il y a 1 min → `is_lost == False`
  - quorum perdu il y a 4 min → `is_lost == True`
  - quorum retrouvé après perte → `is_lost == False` et `lost_since == None`
  - patroni injoignable depuis 4 min → `is_lost == True`
- Budget P4 : 4 tests
```

---

#### A2.2.b — `[INV-085] Email direction technique sur quorum perdu (initial)`

**Labels** : `ai:queue`

**Dépend de** : A2.2.a mergée.

**Body** :

```markdown
## Invariant
INV-085 (C, 🐛, sous-cas 2/3) — Quand `evaluate_quorum_loss` détecte la
perte (≥ 3 min), envoyer **1 email initial** à `system_config.alert_email`.

## Scope de cette sous-issue
1. Quand `evaluate_quorum_loss` passe de `is_lost=False` à `is_lost=True`
   et qu'aucun email n'a encore été envoyé pour cet incident :
   - Lire `alert_email` depuis `SystemConfig`
   - Envoyer via `email_service.send_email(...)` (helper existant — réutiliser)
   - Persister `quorum_email_sent_at = now()` pour ne pas re-envoyer

2. Quand `is_lost` retourne à `False` (quorum retrouvé) :
   - Reset `quorum_email_sent_at` et `lost_since` à NULL

## Zone autorisée
- `backend/app/logic/quorum_detection.py` (étendre la fonction pure pour
  retourner également des "actions" : `email_to_send: bool`)
- `backend/app/escalation.py` ou code qui orchestre le tick (déclencher
  `email_service.send_email`)
- `backend/app/email_service.py` UNIQUEMENT si une nouvelle fonction
  utilitaire est nécessaire (ex: `send_quorum_alert`)
- Tests : `tests/unit/` (fonction pure) et `tests/test_e2e.py` (Mailhog)

## Hors-scope
- Reminders (A2.2.c)
- Refactor du service email
- Modifier le contenu visuel de l'email au-delà d'un sujet/corps clair

## Critères de succès
- Test unit : `evaluate_quorum_loss` retourne `email_to_send=True` au moment
  où le seuil est franchi, et `False` aux ticks suivants tant que pas reset
- Test E2E avec Mailhog : stopper 2/3 etcd → attendre → email arrive (1 seul)
- 2-3 tests
- Budget P4 : 3 tests
```

---

#### A2.2.c — `[INV-085] Anti-spam : reminders quorum à 1h / 3h / 6h`

**Labels** : `ai:queue`

**Dépend de** : A2.2.b mergée.

**Body** :

```markdown
## Invariant
INV-085 (C, 🐛, sous-cas 3/3) — Après l'email initial (A2.2.b), envoyer
des reminders à **1h, 3h, 6h** après le premier email, tant que le quorum
n'est pas retrouvé. Aucun reminder après 6h (volontaire — on assume
qu'à ce stade l'opérateur est au courant).

## Scope de cette sous-issue
1. Étendre `evaluate_quorum_loss` (ou nouvelle fonction pure
   `evaluate_quorum_reminder(state, now) -> bool`) pour décider si un
   reminder est dû.
2. Persister `quorum_reminders_sent_at: list[datetime]` (ou compteur +
   dernier timestamp).
3. Logique :
   - Si `is_lost=True` ET `now - quorum_email_sent_at >= 1h` ET
     pas encore de reminder à 1h → envoyer + marquer
   - Idem pour 3h et 6h
   - À la résolution : reset tous les flags

## Zone autorisée
- `backend/app/logic/quorum_detection.py`
- Orchestrateur tick (idem A2.2.b)
- Tests : `tests/unit/` (fonction pure paramétrée sur les seuils)

## Hors-scope
- Changer les seuils (1h/3h/6h sont fixés par le catalogue)
- Anti-spam pour d'autres types d'emails

## Critères de succès
- 4-5 tests unit paramétrés :
  - À 30 min : pas de reminder
  - À 65 min : reminder 1h dû
  - À 65 min mais 1h déjà envoyé : pas dû
  - À 3h05 : reminder 3h dû
  - À 6h05 : reminder 6h dû
  - À 7h : aucun reminder dû (stop après 6h)
  - Quorum retrouvé : tous les flags reset
- Budget P4 : 5 tests
```

---

## Pile A3 — hors-bot (touche denylist ou nécessite chaos infra)

> Ces issues sont créées avec label `human-required` (PAS `ai:queue`).
> Le cron ne les piochera pas. Elles attendent un humain ou une session
> Claude interactive.

### A3.1 — `[INV-076] Workflow CI dédié ENABLE_TEST_ENDPOINTS=false`

**Labels** : `human-required`

**Body** :

```markdown
## Invariant
INV-076 (C, ❌) — En production, `ENABLE_TEST_ENDPOINTS=false` doit faire
retourner 404 à tous les `/api/test/*`.

## Pourquoi hors-bot
Nécessite modifier `.github/workflows/` (denyliste bot, auto-référence
dangereuse).

## Scope
1. Nouveau job dans `pr.yml` (ou workflow séparé `prod-config-check.yml`)
   qui :
   - Lance le backend avec `ENABLE_TEST_ENDPOINTS=false`
   - Vérifie que `GET /api/test/reset` → 404
   - Vérifie que `GET /api/test/advance-clock?minutes=1` → 404
   - Vérifie que `POST /api/test/send-alarm` → 404
2. Job rapide (~30s). Required check.

## Hors-scope
- Modifier la logique de `ENABLE_TEST_ENDPOINTS` dans le backend (déjà
  implémentée — c'est la couverture CI qui manque)

## Critères de succès
- Workflow `prod-config-check.yml` créé, vert sur master
- Fail si quelqu'un retire le gating dans le code backend
```

---

### A3.2 — `[INV-093] Test split-brain (toxiproxy ou partition Patroni)`

**Labels** : `human-required`

**Body** :

```markdown
## Invariant
INV-093 (H, ❌) — Aucun scénario de partition réseau ne doit produire
2 primaries simultanés.

## Pourquoi hors-bot
- Touche `infra/` (denyliste) si on installe toxiproxy
- Nécessite scénario chaos non couvert par la suite actuelle
- Risque de casser le cluster CI si mal cadré

## Scope
1. Installer toxiproxy (ou un mécanisme `iptables`/`tc`) dans le compose CI
2. Test qui :
   - Coupe la communication entre n1 et {n2, n3}
   - Attend 60s
   - Vérifie via `/api/cluster` qu'il n'y a qu'**un seul** primary
3. Workflow séparé (tier 4 chaos / nightly), pas de gating PR

## Hors-scope
- Implémenter le fix si un split-brain est détecté (c'est Patroni qui gère)
- Test de récupération post-partition (autre invariant)

## Critères de succès
- Test reproductible localement (script de démo)
- Vert en CI nightly
- Documentation du run dans `docs/AI_STRATEGY.md` section tier 4
```

---

### A3.3 — `[INV-095] Test écriture atomique au failover`

**Labels** : `human-required`

**Body** :

```markdown
## Invariant
INV-095 (H, ❌) — Si le primary crash pendant un `POST /alarms/send`,
soit l'alarme est commitée (visible partout après failover), soit pas
du tout. Pas d'état intermédiaire.

## Pourquoi hors-bot
Identique à A3.2 : chaos infra, risque cluster CI.

## Scope
1. Test qui :
   - Lance un `POST /alarms/send` long (instrumenté via un délai injectable
     dans le code, ou via un fixture mock)
   - Au moment où il est "en cours", `docker compose kill backend-primary`
   - Attend l'élection nouveau primary (~30s)
   - Vérifie : `GET /alarms/active` sur le nouveau primary → soit présent,
     soit absent (jamais un partial)

## Hors-scope
- Garantir le succès dans 100% des cas (PostgreSQL fait son job — on
  vérifie juste l'atomicité observable)

## Critères de succès
- Test 5/5 stable (pas de flake)
- Vert en CI nightly chaos
```

---

### A3.4 — `[INV-018b] Frontend index.html : timeAgo basé sur original_created_at`

**Labels** : `human-required`

**Dépend de** : A2.1.a et A2.1.b mergées (le champ existe et est exposé).

**Body** :

```markdown
## Invariant
INV-018b (C, 🐛, scope frontend) — `backend/app/templates/index.html`
(lignes 567 et 575) doit utiliser `alarm.original_created_at` au lieu de
`alarm.created_at` pour l'affichage timeAgo.

## Pourquoi hors-bot
`backend/app/templates` est dans la denylist du bot (phase 1 : "bot scope
backend uniquement").

## Scope
1. Dans `backend/app/templates/index.html` :
   - Remplacer `alarm.created_at` par `alarm.original_created_at` aux
     lignes 567 et 575 (et tout autre call site équivalent — chercher
     `created_at` dans le fichier)
2. Tester manuellement : créer une alarme, l'escalader plusieurs fois,
   vérifier que le "il y a X min" affiche l'âge depuis la création, pas
   depuis la dernière escalade.

## Hors-scope
- Backend (fait dans A2.1.a, A2.1.b, A2.1.c)
- Refonte UI du dashboard

## Critères de succès
- Test E2E `tests/test_frontend.py` ajoute un assert sur le rendu timeAgo
  après escalade
- Visuellement OK sur cluster dev
```

---

## Mapping résumé

| Pile | Issues | Label |
|---|---|---|
| **A1 atomiques** | 8 (082, 084×2, 074, 077, 078, 007, 110) | `ai:queue` |
| **A2 décomposées** | 6 (INV-018 ×3 + INV-085 ×3) | `ai:queue` |
| **A3 hors-bot** | 4 (076, 093, 095, 018b-frontend) | `human-required` |
| **Total** | 18 | — |

À cadence cron 4h × 1 issue par tick : ~3 jours pour épuiser A1+A2 (14 issues).
A3 reste pour humain / sessions interactives.

## Ordre de pioche recommandé

Le cron pioche "la plus ancienne `ai:queue`". Pour contrôler l'ordre, créer
les issues dans cet ordre (de la 1ère à la dernière, à 1 sec d'intervalle) :

1. A1.1 — INV-082 (test seul, ★ rapide, valide le pipeline cron)
2. A1.7 — INV-007 (test seul, ★ rapide)
3. A1.8 — INV-110 (test seul, ★ rapide)
4. A1.4 — INV-074 (test, ★ rapide)
5. A1.6 — INV-078 (test + petit fix possible)
6. A1.5 — INV-077 (test + petit fix possible)
7. A1.2 — INV-084 watchdog (fix code + test, ★★)
8. A1.3 — INV-084 escalation_tick + watchdog_tick (2 clés séparées, ★★)
9. A2.1.a — INV-018 modèle + écriture (★★ migration DB)
10. A2.1.b — INV-018b lectures /alarms (★★, dépend de 9)
11. A2.1.c — INV-018b stats (★★, dépend de 9)
12. A2.2.a — INV-085 détection (★★)
13. A2.2.b — INV-085 email initial (★★, dépend de 12)
14. A2.2.c — INV-085 reminders (★★, dépend de 13)

Pour A3 (human-required, pas piochés par cron) : créer dans n'importe quel
ordre.

## Notes de revue avant création

- ★ Les sous-issues A2.1.b/c dépendent de A2.1.a mergée — si on les crée
  toutes d'un coup, le bot peut piocher b/c avant que a soit mergé. **Option** :
  ne créer A2.1.b/c qu'après merge de A2.1.a (humain qui surveille), ou bien
  ajouter dans le body "ATTENDRE merge de A2.1.a — sinon abandonner et
  commenter". Le bot lit le body donc cette consigne devrait être respectée.
- Idem pour la chaîne A2.2.a → b → c.
- A1.4-A1.8 sont des tests "verrouille un comportement actuel" — si le test
  passe vert dès la première itération, le body invite à mettre à jour le
  catalogue (passer ⚠️ → ✅). Mais le bot **ne peut pas** éditer
  `tests/INVARIANTS.md` (denyliste). Donc dans ce cas il commit juste le test
  et l'humain met à jour le catalogue à la review.
