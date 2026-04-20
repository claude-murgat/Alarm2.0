# Audit du catalogue INVARIANTS.md — 2026-04-20

## Méthode

Audit déclenché après le pilote phase 2 du bot IA (issue #6 / INV-031) :
l'agent a abandonné proprement en constatant que le bug marqué 🐛 était déjà
fixé dans le code (`cc35b7d`). Hypothèse : d'autres invariants sont dans le
même cas.

Travail effectué (~90 min) :
1. Lecture `.claude/CLAUDE.md`, `docs/AI_STRATEGY.md` (sections 1/2/4),
   `tests/INVARIANTS.md`, `tests/audit_v2.json`, `.github/ai-bot/README.md`.
2. Lecture ciblée des fichiers backend impactés par chaque invariant :
   `backend/app/escalation.py`, `backend/app/logic/*.py`,
   `backend/app/api/alarms.py`, `backend/app/api/config.py`,
   `backend/app/api/test_api.py`, `backend/app/api/users.py`,
   `backend/app/api/stats.py`, `backend/app/watchdog.py`,
   `backend/app/models.py`, `backend/app/database.py`, `backend/app/main.py`,
   `backend/app/schemas.py`, `backend/app/templates/index.html`.
3. Grep sur les symboles clés : `original_created_at`, `clock_now`,
   `fcm_escalation_delay_minutes`, `has_quorum`, `_check_rate_limit`,
   `ENABLE_TEST_ENDPOINTS`, `escalation_count`, `hors_heures`.
4. Run `pytest tests/unit -m unit -q` : **87 passed en 0.22s** (tier 1 vert).
5. Pas de lancement tier 2/3 (pas nécessaire pour cet audit statique).

Règle appliquée : **le code de master tranche**. Si le catalogue contredit le
code, c'est le catalogue qui doit évoluer. Aucun fichier source modifié ni
test écrit.

## Synthèse

- **21 invariants audités** (9 priorité 1 + 12 priorité 2).
- **2 invariants 🐛 confirmés bug actuel** : INV-018, INV-018b (ainsi que 3
  sous-cas au sein de INV-084 et INV-085).
- **1 invariant 🐛 dont le catalogue est incorrect** : INV-082 (le code EST
  atomique, seul le test de race manque → 🐛 doit devenir ⚠️).
- **1 invariant ❌ en réalité testé** : INV-073 (rate limiting login — test
  déjà présent dans `tests/test_improvements.py`).
- **3 invariants ❌ déjà ✅ dans le catalogue actuel** : INV-014, INV-016,
  INV-055. La liste de priorités de la mission est antérieure à la dernière
  mise à jour du catalogue (`fbb26a6 docs(invariants): mise a jour post PR 0-5`).
- **2 erreurs de détail dans le catalogue** concernant
  `fcm_escalation_delay_minutes` : la clé n'est PLUS dans le seed de
  `main.py:65-67` (contrairement à ce qu'affirment INV-011 ligne 101 et
  INV-084 ligne 392).
- **Bilan bot IA** : 4 invariants 🐛 sont encore de vrais bugs adressables
  par le bot (INV-018+018b, INV-084 partiel, INV-085). Le bot abandonnerait
  à juste titre sur INV-011, INV-015b, INV-031, INV-066, INV-082.

## Détail par invariant

### INV-011 [C] Délai d'escalade uniforme 15 min
- **Statut actuel dans INVARIANTS.md** : ✅ (fixé PR2).
- **Statut réel dans le code** : ✅ fixé. Confirmé.
- **Preuve** :
  - `grep fcm_escalation_delay_minutes backend/` → **0 matches**. La clé
    n'est plus lue nulle part.
  - `backend/app/escalation.py:190-193` charge
    `escalation_delay_minutes` unique depuis `SystemConfig`, le passe à
    `evaluate_escalation` en paramètre unique.
  - `backend/app/logic/escalation.py:27-33` signature :
    `delay_minutes: float` (un seul délai, pas de split position).
  - `backend/app/main.py:65-67` seed de `SystemConfig` : seuls
    `escalation_delay_minutes`, `watchdog_timeout_seconds`,
    `sms_call_delay_minutes` sont seedés. **`fcm_escalation_delay_minutes`
    n'est PAS présent.**
  - Tests : `tests/unit/test_escalation.py::TestUniformDelay` (6 tests
    paramétrisés) + `tests/test_fcm.py::test_no_fast_escalation_for_veille_user_inv011`.
- **Proposition de mise à jour du catalogue** :
  - Conserver ✅.
  - **Corriger ligne 101** : retirer « Clé encore présente en DB mais plus
    utilisée → à supprimer du seed ». Réalité : la clé n'est déjà plus
    seedée.
  - **Corriger tableau INV-084 ligne 392** : retirer la ligne
    `fcm_escalation_delay_minutes | — | ... | ⚠️ clé encore seedée mais non
    lue`. Ou la reformuler en « ✅ jamais seedée, jamais lue — rien à
    faire ».
  - **Corriger question ouverte §❓ ligne 515** : le dilemme « on la retire
    du seed ou on la garde » n'a plus de sens puisqu'elle n'y est pas.

### INV-015b [C] Tentative FCM wake-up avant escalade si user offline
- **Statut actuel dans INVARIANTS.md** : ✅ (fixé PR2).
- **Statut réel dans le code** : ✅ fixé.
- **Preuve** :
  - `backend/app/logic/escalation.py:62-64` : `wake_ups.append(FCMWakeUp(...))`
    si `users_online.get(alarm.assigned_user_id, True) == False`.
  - `backend/app/escalation.py:211-219` applique `actions.wake_ups` AVANT
    les `actions.escalations` (ordre préservé).
  - Tests : `tests/unit/test_escalation.py::TestFcmWakeUpInv015b` (4 tests).
- **Proposition** : conserver ✅ tel quel.

### INV-018 [C] Ajouter `alarm.original_created_at` immuable
- **Statut actuel dans INVARIANTS.md** : 🐛.
- **Statut réel dans le code** : 🐛 encore buggy.
- **Preuve** :
  - `grep original_created_at backend/` → **0 matches**.
  - `backend/app/models.py:23-43` (classe `Alarm`) : seule colonne `created_at`
    (ligne 38, default `datetime.utcnow`). Pas de colonne
    `original_created_at`.
  - `backend/app/escalation.py:157` (ack expiry) et `:245` (escalade) :
    `alarm.created_at = now` réécrit systématiquement le timestamp, donc
    toute donnée historique est perdue à la première escalade.
- **Proposition** : conserver 🐛. Invariant encore valide, pas de PR.
  Reste le gros chantier listé au backlog ligne 526.

### INV-018b [C] Toute lecture historique utilise `original_created_at`
- **Statut actuel dans INVARIANTS.md** : 🐛.
- **Statut réel dans le code** : 🐛 encore buggy.
- **Preuve** : les 6 call sites listés dans le catalogue lisent encore
  `created_at` (logique : `original_created_at` n'existe pas côté modèle) :
  - `backend/app/api/alarms.py:127` : `Alarm.created_at >= since` (filtre
    historique `/alarms/?days=N`).
  - `backend/app/api/alarms.py:142` : `.order_by(Alarm.created_at.desc())`
    pour `/alarms/active`.
  - `backend/app/schemas.py:124` : `created_at=alarm.created_at` exposé
    dans `AlarmResponse`.
  - `backend/app/api/stats.py:108,120,123,143-145` : tous les calculs KPI
    (bucket semaine, filtre hors heures, MTTR) lisent `alarm.created_at`.
  - `backend/app/templates/index.html:567, 575` :
    `${timeAgo(a.created_at)}` (affichage historique front).
- **Corollaire observé** : comme INV-018 réécrit `created_at` à chaque
  escalade, une alarme escaladée 2h après création apparaît bien « il y a
  2 minutes » dans `/alarms` + KPI biaisés exactement comme décrit.
- **Proposition** : conserver 🐛. Le catalogue reste la meilleure référence
  pour le futur PR de migration. Rafraîchir les numéros de ligne lors du
  PR :
  - `alarms.py:116-117` → réel `alarms.py:127` (filtre).
  - `alarms.py:131` → réel `alarms.py:142` (ORDER BY /active).
  - Les autres numéros correspondent toujours, à la ligne près.

### INV-031 [C] Seuls les users notifiés peuvent acquitter
- **Statut actuel dans INVARIANTS.md** : ✅ (fixé PR5, `cc35b7d`).
- **Statut réel dans le code** : ✅ fixé. Confirmé (concordance avec
  l'abandon propre du bot sur l'issue #6).
- **Preuve** :
  - `backend/app/logic/ack_authorization.py` : fonction pure
    `evaluate_ack_authorization` en place.
  - `backend/app/api/alarms.py:177-190` : vérification appelée dans
    `acknowledge_alarm`, raise HTTP 403 si `auth.allowed == False`.
  - Tests : `tests/unit/test_ack_authorization.py` (7 tests).
- **Proposition** : conserver ✅ tel quel.

### INV-066 [H] Références temporelles via `clock_now()`
- **Statut actuel dans INVARIANTS.md** : ✅ (fixé PR3, 3 call sites
  `notified_at`).
- **Statut réel dans le code** : ✅ fixé sur le périmètre PR3, **avec un
  écart latent à signaler**.
- **Preuve du fix** (les 3 call sites listés dans le catalogue) :
  - `backend/app/escalation.py:43` : `_add_notified_user` → `clock_now()`.
  - `backend/app/api/alarms.py:27` : idem (avec commentaire « INV-066 »).
  - `backend/app/api/test_api.py:78` (send_test_alarm) et `:249`
    (trigger_escalation) : idem avec commentaire explicite.
- **Écart latent (hors périmètre PR3 mais couvert par la formulation
  « toute écriture de timestamp »)** :
  - `backend/app/api/alarms.py:84-89` (`send_alarm`) : l'objet `Alarm`
    créé ne passe PAS `created_at=clock_now()`. Retombée sur le default
    SQLAlchemy `datetime.utcnow` (models.py:38).
  - En pratique les tests existants créent l'alarme AVANT
    `advance-clock`, donc cet écart ne déclenche pas de faux positif.
    Mais un futur test qui ferait `advance-clock` *puis* `POST /alarms/send`
    verrait une alarme avec `created_at` en « temps réel » et les calculs
    d'elapsed tomberaient en négatif.
  - Pas de test à écrire aujourd'hui, mais à flagger pour le jour où
    INV-018 sera implémenté (ce sera le bon moment pour forcer
    `clock_now()` à la création aussi).
- **Proposition** :
  - Conserver ✅.
  - **Compléter ligne 308-313** par un paragraphe « Écart latent connu » :
    > Le call site `send_alarm` (`alarms.py:84`) laisse SQLAlchemy remplir
    > `created_at` via `default=datetime.utcnow`. Non problématique tant que
    > les tests créent l'alarme avant tout `advance-clock`. À forcer en
    > `clock_now()` lors de l'implémentation d'INV-018.

### INV-082 [H] `/config/escalation/bulk` atomique
- **Statut actuel dans INVARIANTS.md** : 🐛.
- **Statut réel dans le code** : ✅ **fonctionnellement atomique** (le
  catalogue se trompe sur le marqueur). Seul manque le test de race.
- **Preuve** :
  - `backend/app/database.py:15` :
    `SessionLocal = sessionmaker(autocommit=False, autoflush=False, ...)`.
  - `backend/app/api/config.py:120-144` (`save_escalation_chain_bulk`) :
    ```
    db.query(EscalationConfig).delete()      # ligne 139
    for i, uid in enumerate(user_ids):
        db.add(EscalationConfig(...))          # lignes 142-143
    db.commit()                                # ligne 144 (un seul commit)
    ```
    Un seul `commit()` → un seul `BEGIN … COMMIT`. PostgreSQL en
    READ COMMITTED (défaut) ne rend visible ni le DELETE ni les INSERTs
    à une session concurrente avant ce commit. Un `GET /config/escalation`
    concurrent voit donc **soit la chaîne d'avant, soit celle d'après**,
    jamais vide (tant que la chaîne d'avant était non-vide).
  - `db.query(...).delete()` est exécuté immédiatement côté SGBD mais à
    l'intérieur de la transaction courante. Pas d'auto-commit.
- **Ce qui manque** : pas de test de race (thread A POST /bulk en boucle +
  thread B GET /escalation en boucle). Le catalogue le décrit correctement
  (ligne 374) mais marque 🐛 au lieu de ⚠️.
- **Proposition** :
  - **Passer 🐛 → ⚠️ ligne 367**.
  - Reformuler l'invariant :
    > INV-082 [H] ⚠️ `/config/escalation/bulk` est atomique (transaction
    > unique)
    >
    > Implémentation : DELETE + INSERTs commités ensemble (`autocommit=False`,
    > un seul `db.commit()`). PostgreSQL READ COMMITTED garantit qu'une
    > lecture concurrente ne voit jamais une liste vide.
    >
    > **Manque un test de race** (thread A bulk / thread B read) pour
    > prouver la propriété en live. C'est le ticket ouvert.
  - **Retirer INV-082 du backlog prioritaire** (ligne 39 et tableau ligne
    527) puisque ce n'est plus un fix code à faire, seulement un test.
    Rester dans le backlog « tests à ajouter ».

### INV-084 [C] Tous les délais métier paramétrables
- **Statut actuel dans INVARIANTS.md** : 🐛.
- **Statut réel dans le code** : 🐛 encore buggy sur 3 constantes.
  Tableau au catalogue globalement correct, **une ligne à corriger**.
- **Preuve** (tableau relu au 2026-04-20) :

  | Clé SystemConfig | Statut catalogue | Réalité code |
  |---|---|---|
  | `escalation_delay_minutes` | ✅ lu en DB | ✅ confirmé (`escalation.py:190-193`) |
  | `sms_call_delay_minutes` | ✅ lu en DB | ✅ confirmé (`escalation.py:268-271`) |
  | `oncall_offline_delay_minutes` | 🐛 hardcodé `escalation.py:17` | 🐛 confirmé **`escalation.py:24`** : `ONCALL_OFFLINE_DELAY_MINUTES = 15.0` |
  | `watchdog_timeout_seconds` | 🐛 seedé mais hardcodé `watchdog.py:14` | 🐛 confirmé (`watchdog.py:14` : `WATCHDOG_TIMEOUT_SECONDS = 60`). La clé est bien seedée (`main.py:66`) mais non lue. |
  | `escalation_tick_seconds` | 🐛 hardcodé `escalation.py:112, 258` | 🐛 confirmé : `await asyncio.sleep(10)` à **`escalation.py:331`** (hors boucle leader) + `asyncio.sleep(30)` à `watchdog.py:48`. Clé non seedée. |
  | `fcm_escalation_delay_minutes` | ⚠️ clé encore seedée mais non lue | **❌ FAUX** : la clé n'est **pas** dans le seed (`main.py:65-67`). Rien à faire. |
  | `ack_suspension_minutes` | ✅ retiré du seed (décision produit) | ✅ confirmé (hardcodé `alarms.py:197`). |

- **Proposition** :
  - Conserver 🐛 pour l'invariant global (3 sous-cas non fixés).
  - **Corriger le tableau ligne 392** : retirer/marquer ✅ la ligne
    `fcm_escalation_delay_minutes`.
  - **Rafraîchir les numéros de ligne** :
    - `oncall_offline_delay_minutes` : `escalation.py:17` → `escalation.py:24`.
    - `escalation_tick_seconds` : `escalation.py:112, 258` → `escalation.py:331`.

### INV-085 [C] Perte de quorum cluster → email direction technique
- **Statut actuel dans INVARIANTS.md** : 🐛.
- **Statut réel dans le code** : 🐛 encore buggy (non implémenté).
- **Preuve** :
  - `backend/app/main.py:216-249` (`/api/cluster`) expose bien
    `quorum.has_quorum` au front (consommé par `index.html:923-925`).
  - `grep send_alert_email backend/` → **2 call sites uniquement** :
    - `escalation.py:385` = INV-053 (oncall all offline).
    - `alarms.py:73` = INV-080 (chaîne vide).
  - Aucun appel basé sur `has_quorum` ou sur un check Patroni périodique.
  - Aucun tâche asyncio de monitoring quorum ailleurs dans le code.
- **Proposition** : conserver 🐛 tel quel. Répondre à la question §❓
  ligne 514 (seuil « Patroni injoignable > X minutes ») pour pouvoir
  spec-er le fix.

### INV-005 [H] escalation_count monotone croissant
- **Statut actuel** : ❌ non testé.
- **Statut réel** : ❌ toujours non testé comme propriété.
- **Preuve** : les grep `escalation_count` dans `tests/` donnent uniquement
  des assertions point-à-point (`== 1`, `== 2`, `>= 1`) dans
  `tests/test_e2e.py`. Pas de test property-based `hypothesis` vérifiant
  que `count[t+1] >= count[t]` sur N opérations mixtes.
  `hypothesis` est installé (`requirements-dev.txt`) mais 0 usage.
- **Proposition** : conserver ❌. Backlog ligne 533 toujours valide.

### INV-014 [H] Escalade ignore `is_online`
- **Priorité 2 de la mission** : INV-014 listé comme ❌. **Catalogue déjà à
  jour** : ligne 118 affiche ✅.
- **Statut réel** : ✅ confirmé.
- **Preuve** : `backend/app/logic/escalation.py:62-72` choisit le
  next_user sans filtrer sur `users_online`. Tests existants :
  `tests/unit/test_escalation.py::test_ignores_online_status_for_next_user_selection`
  + E2E `TestEscalationReachesOffline` (2 tests).
- **Proposition** : pas de changement — le catalogue est déjà correct, la
  liste de priorités de la mission est antérieure à `fbb26a6`.

### INV-016 [H] Expiration d'ACK réactive l'alarme
- **Priorité 2 de la mission** : listé ❌. **Catalogue à jour** ligne 140 :
  ✅.
- **Statut réel** : ✅ confirmé.
- **Preuve** : logique dans `backend/app/logic/ack_expiry.py`, appliquée à
  `backend/app/escalation.py:148-169`. Tests :
  `tests/unit/test_ack_expiry.py` (12 tests) + E2E
  `TestAlarmAcknowledgement::test_ack_expiry_reactivates_alarm` et
  `test_ack_expiry_escalation_restarts`.
- **Proposition** : pas de changement.

### INV-017 [M] Reset SMS/Call après réactivation ACK
- **Priorité 2 de la mission** : listé ❌. **Catalogue** : ⚠️ (pas ❌).
- **Statut réel** : ⚠️ confirmé (code OK, test unit dédié absent).
- **Preuve** : `backend/app/escalation.py:164-167` reset bien
  `notif.sms_sent`, `.call_sent`, `.notified_at`. Pas de test isolé qui
  force le cycle ack → expiry → re-escalade et vérifie les 3 champs.
- **Proposition** : conserver ⚠️. Backlog possible : « 1 test E2E dédié ».

### INV-019 [M] Chaîne d'escalade : positions uniques → rejet
- **Statut actuel** : ❌ non testé.
- **Statut réel** : **⚠️ implémentation divergente** — la règle est
  réellement « upsert silencieux », pas « rejet 409 ».
- **Preuve** : `backend/app/api/config.py:19-41` (`POST
  /config/escalation`) : si `existing = ... position == config.position`
  → réassigne `user_id`/`delay_minutes` et commit. **Pas de 409**.
  L'endpoint bulk (`/bulk`, ligne 120-150) auto-numérote les positions à
  partir de 1, donc pas de collision possible côté bulk.
- **Proposition** :
  - Option A (recommandée) : conserver 🐛 / ❌ et créer une issue
    dédiée pour faire évoluer le code vers un rejet 409.
  - Option B : **reformuler l'invariant** pour refléter la réalité
    « `POST /config/escalation` est idempotent : position déjà occupée →
    200 avec upsert du user_id ». Mais cela va à l'encontre de la raison
    business (« éviter mauvaise manip silencieuse »).
  - Décision appartient au propriétaire. Le code actuel est inchangé
    depuis longtemps — c'est peut-être simplement une règle qui n'a jamais
    été appliquée.

### INV-020 [M] Chaîne d'escalade : user_id uniques
- **Statut actuel** : ❌ non testé.
- **Statut réel** : ⚠️ partiel — bulk OK, single endpoint non.
- **Preuve** :
  - Bulk (`config.py:130-131`) : rejet 422 si `len(user_ids) != len(set(user_ids))`. ✅.
  - Single (`config.py:19-41`) : aucun check user_id unique. Un admin peut
    mettre user1 en position 1 et 2 via deux POST successifs.
- **Proposition** : conserver ❌ ou passer ⚠️. Clarifier que bulk est OK,
  single ne l'est pas. Fix code minime (ajouter un check user_id).

### INV-055 [M] Oncall : seul position 1 déclenche la surveillance
- **Priorité 2 de la mission** : listé ❌. **Catalogue** : ✅ ligne 258.
- **Statut réel** : ✅ confirmé.
- **Preuve** : `tests/unit/test_oncall.py::TestInv055OnlyPos1IsMonitored`
  + E2E `test_no_oncall_alarm_if_not_position1`.
- **Proposition** : pas de changement.

### INV-073 [H] Rate limiting login
- **Statut actuel dans INVARIANTS.md** : ❌.
- **Statut réel dans le code** : ✅ **implémenté ET testé**.
- **Preuve** :
  - Code : `backend/app/api/users.py:18-32` (`_check_rate_limit`,
    `RATE_LIMIT_MAX_FAILURES = 10`, `RATE_LIMIT_WINDOW = 60`).
    Appelé à la ligne 62 (`login`), raise 429 au 11e échec.
  - Test : `tests/test_improvements.py:405-424`
    (`TestRateLimiting::test_login_rate_limited_after_many_failures`
    + `test_legitimate_login_still_works_after_rate_limit`).
- **Proposition** :
  - **Passer ❌ → ✅ ligne 329**.
  - Remplacer « Test : aucun actuellement » par :
    > Tests : `tests/test_improvements.py::TestRateLimiting` — 11e
    > tentative renvoie 429, puis login légitime refonctionne.
  - **Retirer INV-073 du backlog prioritaire** (tableau ligne 529).

### INV-076 [C] `ENABLE_TEST_ENDPOINTS=false` → /api/test/* renvoie 404
- **Statut actuel** : ❌.
- **Statut réel** : ❌ **toujours non testé au sens strict**, même si une
  sonde indirecte existe.
- **Preuve** :
  - Code : `backend/app/api/test_api.py:13-21` : guard en place,
    `_require_test_endpoints` raise 404.
  - Test partiel : `tests/test_improvements.py:237-264` vérifie que
    `/health` expose le flag `test_endpoints_enabled` à `True` (état de
    test). **Ne teste PAS** la branche `false` : aucun job CI ne tourne
    avec `ENABLE_TEST_ENDPOINTS=false`.
- **Proposition** : conserver ❌. Le backlog ligne 532 (« job CI dédié »)
  reste l'action correcte.

### INV-093 [H] Pas de split-brain
- **Statut actuel** : ❌ (avec note « Non couvert aujourd'hui »).
- **Statut réel** : ❌ inchangé. Tier 4 chaos toujours non implémenté
  (`AI_STRATEGY.md` section 3).
- **Proposition** : conserver.

### INV-095 [H] Écriture atomique au failover
- **Statut actuel** : ❌ avec mention « 🐛 Non testé » ligne 433.
- **Statut réel** : ❌ inchangé. Nécessite tier 4 chaos (Patroni kill
  pendant écriture).
- **Proposition** : conserver. La mention « 🐛 Non testé » est OK mais
  mélange sémantique « test manquant » et marqueur bug — à la limite
  remplacer par « ❌ Non testé (nécessite tier 4 chaos) ».

### INV-111 [M] Filtre "hors heures France"
- **Statut actuel** : ❌ (avec « peu couvert »).
- **Statut réel** : ❌ confirmé — code présent
  (`backend/app/api/stats.py:66-91` + `_est_jour_ferie_fixe`, `_paques`),
  aucun test unit ou E2E ne force une date en jour férié / heure creuse.
- **Proposition** : conserver ❌. Bon candidat pour tier 1 (fonctions
  pures déjà en place, 3-4 tests paramétrés suffiraient).

## Proposition de diff INVARIANTS.md

Patch unifié prêt à appliquer. Le propriétaire peut le passer en
`git apply` (ou appliquer à la main) puis commit. **Aucune modification de
code, uniquement le catalogue.**

```diff
--- a/tests/INVARIANTS.md
+++ b/tests/INVARIANTS.md
@@ -17,7 +17,7 @@
 **Extraction logique pure** : PR 1-5 terminés (cc35b7d sur master).
 - 87 unit tests en 0.14s (logic/ entièrement couvert)
 - Suite E2E : 239 passed / 17 skipped / 0 fail en 1h09
-- 5 bugs catalogue corrigés : INV-011, INV-015b, INV-031, INV-066, INV-102
+- 5 bugs catalogue corrigés : INV-011, INV-015b, INV-031, INV-066, INV-102 + INV-073 (rate limit déjà testé, cf. audit 2026-04-20)
 
 **Statut par catégorie** :
 
@@ -36,7 +36,7 @@
 | 10. Observabilité | 1 | 2 | 0 | 0 (INV-102 ✅ PR0) |
 | 11. Stats | 0 | 2 | 0 | 0 |
 
-**Restant prioritaire** : INV-018 (`original_created_at`), INV-082 (atomicité bulk), INV-084 (délais paramétrables), INV-073 (rate limiting), INV-085 (quorum email).
+**Restant prioritaire** : INV-018 (`original_created_at`), INV-084 (délais paramétrables hardcodés), INV-085 (quorum email). INV-082 déjà atomique (manque juste un test de race). INV-073 déjà testé (cf. audit 2026-04-20).
 
 ---
 
@@ -98,7 +98,7 @@
 ### INV-011 [C] ✅ Délai d'escalade uniforme : 15 min pour chaque user
 Le délai entre chaque palier d'escalade est de `escalation_delay_minutes` (défaut 15) pour **tous** les users de la chaîne, quelle que soit leur position.
 - **Pourquoi** : chaque humain a droit au même temps pour répondre. Pas de "veille" accélérée.
-- **Fix** : PR2 (df9a43a). La lecture de `fcm_escalation_delay_minutes` a été retirée de `escalation.py`. Clé encore présente en DB mais plus utilisée → à supprimer du seed (voir INV-084).
+- **Fix** : PR2 (df9a43a). La lecture de `fcm_escalation_delay_minutes` a été retirée de `escalation.py`. La clé n'est plus seedée dans `main.py` non plus (vérifié 2026-04-20). Rien à nettoyer.
 - **Couverture** :
   - Unit : `test_escalation.py::TestUniformDelay` (6 tests paramétrisés position 1/2/3 × below/above delay)
   - E2E : `test_fcm.py::test_no_fast_escalation_for_veille_user_inv011` (assertion inversée de l'ancien test)
@@ -306,7 +306,7 @@
 
 **Invariant** : toute écriture de timestamp (`notified_at`, `created_at`, `last_heartbeat`, `acknowledged_at`, `suspended_until`, etc.) doit utiliser `clock_now()`. Sinon, en test, une partie des timestamps est "temps simulé" et une autre est "temps réel" → incohérences.
 
-**Fix** : PR3 (0716174). 3 call sites corrigés :
+**Fix** : PR3 (0716174). 3 call sites `notified_at` corrigés :
 - `alarms.py::_add_notified_user` : ajout `notified_at=clock_now()`
 - `test_api.py::send_test_alarm` (POST `/api/test/send-alarm`) : idem
 - `test_api.py::trigger_escalation` (POST `/api/test/trigger-escalation`) : idem
@@ -313,6 +313,8 @@
 
 **Couverture** : l'invariant est maintenant respecté dans tous les call sites. Pas de test unit dédié (la fonction pure ne voit que des snapshots avec dates déjà calculées), mais les tests E2E qui combinent advance_clock + création d'alarme valident implicitement.
 
+**Écart latent connu (audit 2026-04-20)** : `send_alarm` dans `alarms.py:84-89` ne passe pas `created_at=clock_now()` explicitement ; l'objet Alarm récupère sa valeur via le default SQLAlchemy `datetime.utcnow` (`models.py:38`). Non problématique tant que les tests créent l'alarme avant tout `advance-clock`. À forcer en `clock_now()` lors de l'implémentation d'INV-018 (ce sera le bon moment, puisqu'on touchera au modèle Alarm).
+
 ---
 
 ## 7. Authentification et sécurité
@@ -326,10 +328,12 @@
 ### INV-072 [M] ⚠️ Nom stocké en lowercase
 POST /auth/register avec "TestUser" → stocké "testuser".
 
-### INV-073 [H] ❌ Rate limiting login
+### INV-073 [H] ✅ Rate limiting login
 Plus de 10 tentatives échouées en 60s pour le même username → 429.
 - **Pourquoi** : protection brute-force.
-- **Test** : aucun actuellement.
+- **Code** : `backend/app/api/users.py:18-32` (`_check_rate_limit`,
+  `RATE_LIMIT_MAX_FAILURES = 10`, `RATE_LIMIT_WINDOW = 60`), appelé ligne 62.
+- **Couverture** : E2E `tests/test_improvements.py::TestRateLimiting` —
+  `test_login_rate_limited_after_many_failures` + `test_legitimate_login_still_works_after_rate_limit`.
 
 ### INV-074 [C] ⚠️ Refresh token produit un nouveau token
 POST /auth/refresh avec token valide → nouveau token différent.
@@ -364,15 +368,20 @@
 POST /config/escalation/bulk → DELETE + INSERT de la chaîne + FCM push à tous les users affectés.
 - **Couverture** : E2E `TestEscalationChainBulk::test_save_escalation_chain_replaces_all`.
 
-### INV-082 [H] 🐛 /config/escalation/bulk est atomique (transaction unique)
-`POST /api/config/escalation/bulk` modifie la chaîne en supprimant TOUTES les règles existantes puis en insérant les nouvelles. Si cette opération n'est pas dans une seule transaction, il existe une **fenêtre de temps (~ms)** pendant laquelle la chaîne est VIDE en base.
+### INV-082 [H] ⚠️ /config/escalation/bulk est atomique (transaction unique)
+`POST /api/config/escalation/bulk` modifie la chaîne en supprimant TOUTES les règles existantes puis en insérant les nouvelles. Si cette opération n'est pas dans une seule transaction, il existerait une **fenêtre de temps (~ms)** pendant laquelle la chaîne serait VIDE en base.
 
 **Pourquoi c'est grave** : pendant cette fenêtre, si une alarme arrive (`POST /alarms/send`), le code voit `chaîne vide` → déclenche INV-080 (email direction technique + fallback user). L'alarme est envoyée à la mauvaise personne, et un email inutile est envoyé, juste parce qu'un admin modifiait la chaîne au mauvais moment.
 
 **Invariant** : DELETE + INSERT doivent être dans une transaction SQL unique. Une lecture (GET /config/escalation) concurrente ne doit jamais voir 0 lignes tant que la précédente chaîne était non-vide.
 
+**État du code (audit 2026-04-20)** : **l'invariant est respecté**.
+`SessionLocal` est configurée `autocommit=False, autoflush=False`
+(`backend/app/database.py:15`). Le handler `save_escalation_chain_bulk`
+(`backend/app/api/config.py:120-144`) exécute DELETE puis INSERTs puis un
+seul `db.commit()` — donc une unique transaction. PostgreSQL READ COMMITTED
+garantit qu'aucune session concurrente ne voit la liste vide avant commit.
+
+**Ce qu'il reste à faire** : écrire le test de race ci-dessous pour
+verrouiller la propriété en régression.
+
 **Test** : thread A fait POST /bulk dans une boucle, thread B fait GET /escalation dans une boucle. B ne doit JAMAIS voir une liste vide (si la chaîne précédente contenait au moins 1 user).
 
 ### INV-083 [H] ⚠️ User supprimé → alarmes actives réassignées
@@ -386,13 +395,13 @@
 | Clé SystemConfig | Défaut | Usage | Statut actuel |
 |---|---|---|---|
 | `escalation_delay_minutes` | 15 | Délai entre chaque palier d'escalade (INV-011) | ✅ lu en DB (pure fn en prend la valeur en paramètre) |
 | `sms_call_delay_minutes` | 2 | Délai avant enqueue SMS/Call après notification (INV-060) | ✅ lu en DB |
-| `oncall_offline_delay_minutes` | 15 | Délai avant qu'oncall offline déclenche alarme (INV-050) | 🐛 **hardcodé** dans `escalation.py:17` (`ONCALL_OFFLINE_DELAY_MINUTES`), pure fn le prend en paramètre |
+| `oncall_offline_delay_minutes` | 15 | Délai avant qu'oncall offline déclenche alarme (INV-050) | 🐛 **hardcodé** dans `escalation.py:24` (`ONCALL_OFFLINE_DELAY_MINUTES`), pure fn le prend en paramètre |
 | `watchdog_timeout_seconds` | 60 | Délai avant marquer user offline (INV-041) | 🐛 seedé mais **hardcodé** dans `watchdog.py:14` |
-| `escalation_tick_seconds` | 10 | Période de la boucle d'escalade | 🐛 **hardcodé** dans `escalation.py:112, 258` |
-| `fcm_escalation_delay_minutes` | — | **À SUPPRIMER du seed** : plus lu depuis PR2 (INV-011 fixé) | ⚠️ clé encore seedée mais non lue |
+| `escalation_tick_seconds` | 10 | Période de la boucle d'escalade | 🐛 **hardcodé** dans `escalation.py:331` |
+| ~~`fcm_escalation_delay_minutes`~~ | — | **Retiré**. Plus lu (PR2, INV-011) ni seedé (vérifié 2026-04-20). Rien à faire. | ✅ |
 | ~~`ack_suspension_minutes`~~ | — | **Non paramétrable** (décision IMPROVEMENTS #13) — 30 min hardcodé voulu | ✅ retiré du seed en PR0 |
@@ -512,10 +521,9 @@
 ## ❓ Questions restantes au propriétaire
 
 1. **INV-085 (quorum)** : le seuil de détection "Patroni injoignable depuis > X minutes" — je suggère 2 min, confirme ou ajuste.
-2. **INV-084 fcm_escalation_delay_minutes** : la clé est encore seedée mais plus lue. On la retire du seed dans un petit PR ménage, ou on la garde pour compatibilité future (répétition FCM pendant l'attente d'escalade) ?
-3. **Suivant** : rédiger `android/INVARIANTS.md` pour les invariants côté client (sonnerie continue, vibration, écran verrouillé, rotation bloquée, reprise post-boot, etc.).
+2. **INV-019 (positions uniques rejet 409)** : le code actuel fait un upsert silencieux (`POST /config/escalation` met à jour la position existante plutôt que de renvoyer 409). Règle business à confirmer — sinon reformuler l'invariant en "upsert idempotent" et marquer ✅.
+3. **Suivant** : rédiger `android/INVARIANTS.md` pour les invariants côté client (sonnerie continue, vibration, écran verrouillé, rotation bloquée, reprise post-boot, etc.).
 
 ---
 
 ## 📊 Backlog des fix prioritaires (post PR 0-5)
@@ -526,10 +534,10 @@
 | INV | Criticité | Complexité | Note |
 |---|---|---|---|
 | INV-018 + INV-018b | C | ★★★ | Ajouter `original_created_at` immuable. Migration DB + 5 call sites (stats, schemas, frontend). Gros PR. |
-| INV-082 | H | ★★ | Transaction atomique `/config/escalation/bulk`. Test concurrence `threading.Thread`. |
+| INV-082 | H | ★ | Test concurrence `threading.Thread` (le code est déjà atomique — cf. audit 2026-04-20). Juste un test à ajouter. |
 | INV-019 + INV-020 | M | ★ | Rejeter positions/user_id dupliqués dans `/config/escalation`. Validation Pydantic + test 409. |
-| INV-073 | H | ★ | Rate limiting login. Le code a déjà `_check_rate_limit`, il manque juste le test E2E (11 tentatives → 429). |
+| ~~INV-073~~ | H | — | Déjà ✅ (test présent, cf. audit 2026-04-20). |
 | INV-084 (reste) | C | ★★ | Migrer `ONCALL_OFFLINE_DELAY_MINUTES`, `WATCHDOG_TIMEOUT_SECONDS`, `escalation_tick_seconds` en SystemConfig. |
 | INV-085 | C | ★★ | Quorum perdu → email. Nécessite un ping Patroni périodique + anti-spam. |
 | INV-076 | C | ★ | Job CI dédié avec `ENABLE_TEST_ENDPOINTS=false` vérifiant que `/api/test/*` renvoie 404. |
 | INV-005 | H | ★ | Test property-based avec hypothesis pour `escalation_count` monotone. |
```

> Note d'application : le diff ci-dessus est construit à la main en lisant
> la version actuelle de `tests/INVARIANTS.md` (commit `fbb26a6` sur master).
> Les numéros de ligne peuvent dériver d'un ou deux si le catalogue est
> retouché entre-temps — dans ce cas, appliquer manuellement bloc par bloc.

## Bugs supplémentaires détectés en passant

Rien de grave, mais à noter :

1. **`POST /config/escalation` non protégé contre user_id dupliqué**
   (`backend/app/api/config.py:19-41`). Un admin peut placer le même user
   à deux positions via deux POST successifs. Le bulk endpoint le refuse
   (ligne 130-131) mais pas le single endpoint. Cohérent avec INV-020
   marqué ❌ dans le catalogue — rien à ajouter, juste à rappeler que
   « non testé » masque en fait « non implémenté côté single endpoint ».

2. **Incohérence mineure sur `Alarm.created_at`** (voir INV-066 écart
   latent plus haut). Ne casse rien aujourd'hui mais serait un faux
   positif si un test faisait `advance-clock` → `POST /alarms/send` sans
   passer par `/api/test/send-alarm`.

3. **`escalation_tick_seconds` double localisation** : à la fois
   `asyncio.sleep(10)` dans `escalation.py:331` et `asyncio.sleep(30)`
   dans `watchdog.py:48`. La deuxième n'est pas listée dans le tableau
   INV-084 (qui parle uniquement de « période de la boucle d'escalade »).
   Si on ajoute la clé `escalation_tick_seconds` en SystemConfig, il faut
   décider si le watchdog la partage (probable : oui, simplifie) ou a la
   sienne (`watchdog_tick_seconds`). À discuter au moment du fix INV-084.

4. **Aucun endpoint de test `/test/evaluate-now`** (mentionné comme
   souhaitable dans `AI_STRATEGY.md` section 3 note sur PR6 et dans
   `audit_v2.json::recommended_architecture::enablers`). Pas un bug — un
   manque reconnu. Le bot IA serait plus fiable dessus que sur
   `trigger-escalation` (cf. BUG-03 d'`audit_v2.json`).

---

**Fin du rapport.** 87/87 unit tests verts au moment de l'audit. Aucun
fichier source ni test modifié. Le propriétaire tranche sur le diff
proposé.
