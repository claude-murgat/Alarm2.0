# Catalogue d'invariants — Alarme Murgat

Ce document est la **source de vérité** pour le comportement attendu du système.
Chaque invariant est une règle qui doit être VRAIE à tout moment (ou après toute opération).

**Pour l'IA qui écrit les tests** : ne lis PAS le code pour écrire un test. Lis un invariant ici,
et écris un test qui vérifie CETTE règle. Si un invariant te semble ambigu, lève une question
plutôt que d'interpréter à partir du code (le code peut être buggy).

**Format** : `INV-XXX` stable. Ne jamais renuméroter.
**Criticité** : `C` (critical, bloque release), `H` (high, bloque PR), `M` (medium), `L` (low).
**Statut** : ✅ vérifié par tests existants, ⚠️ partiellement couvert, ❌ non testé, 🐛 bug connu.

---

## État d'avancement global (mis à jour au 2026-05-13)

**Extraction logique pure** : PR 1-5 terminés (cc35b7d sur master).
- 87 unit tests en 0.14s (logic/ entièrement couvert)
- Suite E2E : 239 passed / 17 skipped / 0 fail en 1h09
- 8 bugs catalogue corrigés : INV-011, INV-015b, INV-031, INV-066, INV-102, INV-073 (audit 2026-04-20) + **INV-019 (PR #20)**, **INV-005 (PR #27)**, **INV-084 oncall_offline (PR #25)** — pilotes bot IA 2026-04-21
- **Session 2026-05-12/13** : Bloc A+B cron pioche 4h opérationnel (PR #87, #88) + 6 INV verrouillés par le bot via le pipeline cron : INV-082 (PR #89), INV-007 (PR #90), INV-110 (PR #91), INV-074 (PR #92), INV-077 (PR #93), INV-078 (PR #95). PR #94 prompt.md ajoute la section "regression-lock" pour éviter les faux-abandons sur invariants déjà respectés.

**Statut par catégorie** :

| Catégorie | ✅ | ⚠️ | ❌ | 🐛 |
|---|---|---|---|---|
| 1. Alarme lifecycle | 3 | 3 | 0 | 0 (INV-007 ✅ PR #90) |
| 2. Escalade | 6 | 2 | 0 | 3 (INV-018, 018b, 019, 020) |
| 3. Acquittement | 1 | 2 | 0 | 0 (INV-031 ✅ PR5) |
| 4. Heartbeat | 0 | 3 | 0 | 0 |
| 5. Astreinte | 6 | 0 | 0 | 0 (tout ✅ PR4) |
| 6. SMS/Calls | 4 | 2 | 0 | 1 (INV-066 ✅ PR3) |
| 7. Auth | 6 | 0 | 2 | 0 (INV-074/077/078 ✅ PRs #92/#93/#95) |
| 8. Config/chaîne | 2 | 2 | 0 | 2 (INV-082 ✅ PR #89 ; reste INV-084 + 085) |
| 9. Cluster HA | 0 | 3 | 2 | 0 |
| 10. Observabilité | 1 | 2 | 0 | 0 (INV-102 ✅ PR0) |
| 11. Stats | 1 | 1 | 0 | 0 (INV-110 ✅ PR #91) |
| 13. Hardware trigger | 3 | 0 | 0 | 0 (INV-120 V2 + INV-122 + INV-123 ✅ PR1 issue #112 — 2026-05-18) |

**Restant prioritaire** : INV-018b résiduel (#78 stats KPI, #85 frontend timeAgo), INV-084 (reste 2/3 sous-cas : `watchdog_timeout_seconds` + `escalation_tick_seconds`), INV-076 (#82 CI test endpoints désactivés), INV-093/095 (#83/#84 split-brain + atomicité failover). INV-085 désormais ✅ (2026-05-28, PR <à venir>).

---

## 1. Alarme — état et cycle de vie

### INV-001 [C] ✅ Une seule alarme active à la fois
À tout instant, il existe **au plus une** alarme avec `status IN ('active', 'escalated')`.
- **Pourquoi** : design explicite (CLAUDE.md). Évite la surcharge cognitive de l'opérateur.
- **Couverture** :
  - E2E : `TestSingleAlarm::test_only_one_alarm_at_a_time` (409 sur 2e alarme)
  - Unit : `test_oncall.py::test_regular_active_alarm_blocks_oncall_creation` (oncall respecte l'unicité)
- **Manque** : test de concurrence réelle (2 POST //) — à ajouter avec threading.

### INV-002 [C] ✅ Alarme acquittée → suspended_until dans le futur
Si `alarm.status = 'acknowledged'` alors `alarm.suspended_until IS NOT NULL` ET `alarm.suspended_until > now()`.
- **Pourquoi** : l'ACK suspend l'escalade pendant X minutes. Sans cette règle, l'alarme ne peut pas se réactiver.
- **Couverture** : E2E `TestAlarmAcknowledgement::test_acknowledge_alarm` vérifie `suspended_until is not None`.

### INV-003 [H] ✅ Durée de suspension = 30 min fixe
`alarm.suspended_until = acknowledged_at + 30 minutes` (valeur hardcodée, **NON paramétrable**).
- **Pourquoi** : décision produit (voir IMPROVEMENTS.md #13 REJETE). La durée d'acquittement est gérée par la supervision en amont, le système d'alarme ne fait que relayer avec un délai fixe.
- **Corollaire** : la clé `ack_suspension_minutes` ne doit PAS exister dans `SystemConfig` (supprimée du seed en PR 0, cf commit a8e1e66).
- **Couverture** : E2E `TestAckedAlarmVisibility::test_ack_remaining_seconds_in_response` vérifie ~1800s.

### INV-004 [C] ⚠️ Alarme active a au moins 1 notifié
Si `alarm.status IN ('active', 'escalated')` alors il existe au moins un `AlarmNotification` lié.
- **Pourquoi** : une alarme sans destinataire n'est pas actionnable.
- **Exception** : chaîne d'escalade vide → email direction technique + alarme persiste avec `assigned_user_id` = fallback user (voir INV-040).

### INV-005 [H] ✅ escalation_count monotone croissant
`alarm.escalation_count` ne peut que croître, jamais décroître, même après ack+réactivation.
- **Pourquoi** : c'est un compteur historique utilisé par les stats.
- **Fix** : PR #27 (pilote bot IA lvl 3, 2026-04-21). Audit a confirmé 3 call sites muteurs par `+= 1` uniquement (`escalation.py:243`, `test_api.py:253`, `calls.py:95`), aucun décrément nulle part. Pas de fix code, test de verrouillage en régression.
- **Couverture** : `tests/unit/test_escalation_count_monotonic.py` (tier 1, 2 tests) :
  - `test_eric_scenario_three_escalations_then_ack_reactivate_escalate` — séquence 3 esc → ack → reactivation → 1 esc, asserts counts = [0,1,2,3,3,3,4] monotone
  - `test_escalation_count_monotone_under_random_operations` — **property-based `hypothesis`** (100 exemples, séquences aléatoires `{escalate, ack, ack_expire, resolve, advance_time}`)
  - Mutation test manuel confirmé : injection `escalation_count=0` dans `_step_ack_expire` → les 2 tests fail, hypothesis shrink à la séquence minimale `['escalate_tick', 'ack', 'ack_expire_tick']`.

### INV-006 [M] ⚠️ Transitions de status valides
Les seules transitions autorisées sont :
- `active → acknowledged | escalated | resolved`
- `escalated → acknowledged | resolved`
- `acknowledged → active` (expiration) | `resolved`
- `resolved → TERMINAL` (aucune transition sortante)
- **Pourquoi** : évite les états zombies (ex: "resolved puis escalated").
- **Test** : tenter chaque transition interdite → rejet ou no-op.

### INV-007 [M] ✅ Alarme resolved n'apparaît plus dans /active ni /mine
`GET /api/alarms/active` et `GET /api/alarms/mine` excluent les `status = 'resolved'`.
- **Pourquoi** : UX app mobile.
- **Fix** : PR #90 (bot IA, 2026-05-12). Aucune modif prod (code déjà conforme) ; ajout de 2 tests de verrouillage en régression.
- **Couverture** : `tests/integration/test_alarm_visibility_inv007.py` (tier 2, 2 tests) :
  - `test_resolved_alarm_excluded_from_active`
  - `test_resolved_alarm_excluded_from_mine`
- **Limites connues** : ne couvre que la transition `active → resolved` directe. Transitions intermédiaires (`acknowledged → resolved`, `escalated → resolved`) et alarmes oncall non testées explicitement — à étoffer en issue follow-up si élargissement souhaité.

---

## 2. Escalade

### INV-010 [C] ✅ Escalade seulement si delay dépassé
`escalation n'a lieu que si (now - alarm.created_at) / 60 >= current_delay`.
- **Pourquoi** : respecter le temps laissé à l'utilisateur pour acquitter.
- **Couverture** :
  - Unit : `test_escalation.py::TestNoEligibleAlarms::test_elapsed_below_delay_no_escalation` + `test_elapsed_exactly_delay_escalates` (boundary >=)
  - E2E : `TestEscalationWithClock::test_no_escalation_before_delay` + `test_escalation_after_delay` + `test_escalation_exactly_at_boundary`

### INV-011 [C] ✅ Délai d'escalade uniforme : 15 min pour chaque user
Le délai entre chaque palier d'escalade est de `escalation_delay_minutes` (défaut 15) pour **tous** les users de la chaîne, quelle que soit leur position.
- **Pourquoi** : chaque humain a droit au même temps pour répondre. Pas de "veille" accélérée.
- **Fix** : PR2 (df9a43a). La lecture de `fcm_escalation_delay_minutes` a été retirée de `escalation.py`. La clé n'est plus seedée dans `main.py` non plus (vérifié 2026-04-20 — rien à nettoyer).
- **Couverture** :
  - Unit : `test_escalation.py::TestUniformDelay` (6 tests paramétrisés position 1/2/3 × below/above delay)
  - E2E : `test_fcm.py::test_no_fast_escalation_for_veille_user_inv011` (assertion inversée de l'ancien test)

### INV-012 [H] ⚠️ Escalade cumulative : les anciens restent notifiés
Après escalade, `notified_user_ids` contient l'ancien assigné ET le nouveau.
- **Pourquoi** : design cumulative (tous sonnent jusqu'à ack).
- **Couverture** : E2E `TestCumulativeEscalation::test_escalated_alarm_still_visible_to_first_user` + `test_alarm_shows_notified_users`.

### INV-013 [C] ✅ Escalade wrap around
Après le dernier de la chaîne, l'escalade repart au premier.
- **Pourquoi** : le signal ne doit jamais s'arrêter.
- **Couverture** :
  - Unit : `test_escalation.py::TestChainNavigation::test_wrap_around_from_last_to_first`
  - E2E : `TestEscalation::test_escalation_wraps_around_after_last_user` + `test_escalation_wrap_continues_cycling` (2 tours)

### INV-014 [H] ✅ Escalade ignore is_online
L'escalade ne filtre PAS sur `is_online`. FCM réveille le destinataire.
- **Pourquoi** : sinon un user offline bloque la chaîne.
- **Couverture** :
  - Unit : `test_escalation.py::test_ignores_online_status_for_next_user_selection`
  - E2E : `TestEscalationReachesOffline::test_escalation_reaches_offline_user` + `test_escalation_wraps_even_all_offline`

### INV-015 [H] ✅ Pas d'escalade d'une alarme acknowledged
Une alarme `status = 'acknowledged'` ne peut pas être escaladée (escalation_count ne change pas).
- **Pourquoi** : ACK doit suspendre l'escalade.
- **Couverture** :
  - Unit : `test_escalation.py::test_acknowledged_alarm_not_escalated` + `test_resolved_alarm_not_escalated`
  - E2E : `TestEscalation::test_no_escalation_if_acknowledged`

### INV-015b [C] ✅ Tentative de réveil FCM avant escalade si user offline
Avant d'escalader vers le user suivant, **SI** `current_user.is_online == False`, un FCM **high priority** est envoyé au user courant pour tenter de le réveiller, puis l'escalade se fait au même tick. Le user courant reste dans `notified_user_ids` et continuera à recevoir les rappels cumulative.
- **Si le user courant est ONLINE** : pas de FCM dédié — sa sonnerie est déjà active depuis la notification initiale.
- **Pourquoi** : un user offline peut revenir online dans les secondes qui suivent le FCM high-priority → il pourra acquitter avant que le suivant ne soit dérangé plus longtemps.
- **Fix** : PR2 (df9a43a). `evaluate_escalation` retourne une liste `wake_ups: tuple[FCMWakeUp, ...]` appliquée AVANT les escalations par `escalation_loop`.
- **Couverture** :
  - Unit : `test_escalation.py::TestFcmWakeUpInv015b` (4 tests : offline→wake-up, online→pas de wake-up, pas d'escalade=pas de wake-up, acknowledged=pas de wake-up)

### INV-016 [H] ✅ Expiration d'ACK réactive l'alarme
Si `suspended_until < now` et `status = 'acknowledged'` → au prochain tick, `status → 'active'` et `created_at = now`.
- **Pourquoi** : pas de ghost acknowledgment.
- **Couverture** :
  - Unit : `test_ack_expiry.py` (12 tests couvrant boundaries, non-ack statuses, multiples, pureté)
  - E2E : `TestAlarmAcknowledgement::test_ack_expiry_reactivates_alarm` + `test_ack_expiry_escalation_restarts`

### INV-017 [M] ⚠️ Reset SMS/Call après réactivation ACK
Après INV-016, toutes les `AlarmNotification.sms_sent` et `.call_sent` sont remises à False pour le cycle suivant.
- **Pourquoi** : la gateway doit re-contacter les users.
- **Couverture** : fait dans le code (`escalation.py` bloc ack expiry) mais pas encore de test unit dédié isolant cet effet de bord.

### INV-018 [C] 🐛 Ajouter `alarm.original_created_at` immuable
Nouveau champ `original_created_at` initialisé à la création et **jamais modifié**. `created_at` continue d'être utilisé comme "timer d'escalade" (reset à chaque escalade).
- **Pourquoi** : séparer la donnée historique (quand l'événement s'est produit) du compteur de logique métier (quand commence le timer actuel).

### INV-018b [C] ⚠️ Toute lecture historique utilise `original_created_at`
Les usages suivants doivent lire `original_created_at`, PAS `created_at` :
- ✅ `alarms.py:133-134` — filtre `/alarms/?days=N` (historique) — PR #114 + #121
- ✅ `alarms.py:149` — ORDER BY pour `/active` — PR #114 + #121
- ✅ `schemas.py:83/129` — champ exposé dans `AlarmResponse` — PR #102
- ✅ `stats.py:107-149` — bucketing KPI par semaine + MTTR + filtre période — PR <à venir 2026-05-29>
- ❌ `index.html:567, 575` — affichage timeAgo dans le front — issue #85 (`human-required`)
- **Statut backend** : ✅ complet (3 sous-issues bot/Claude mergées). **Statut frontend** : ⚠️ reste #85.
- **Impact actuel** (frontend) : une alarme escaladée 2h après création apparaît comme "il y a 2min" dans l'historique web (le backend renvoie pourtant `original_created_at` correct dans `AlarmResponse`).
- **Seul usage qui garde `created_at` (timer)** : `escalation.py:185` (calcul `elapsed` pour décision d'escalade), `escalation.py:132, 204` (reset après ack expiry et escalade), `calls.py:96` (reset DTMF escalate).

### INV-019 [M] ✅ Chaîne d'escalade : positions uniques → rejet 409
Dans `EscalationConfig`, `position` est unique. POST /config/escalation avec position déjà occupée → **rejet 409 Conflict**, pas d'upsert silencieux.
- **Pourquoi** : éviter qu'une mauvaise manip écrase silencieusement un user existant.
- **Fix** : PR #20 (pilote bot IA lvl 1, 2026-04-21). `config.py:19-41` faisait un upsert silencieux, remplacé par `raise HTTPException(409, ...)` avec message actionnable (suggère DELETE puis POST, ou endpoint `/bulk`).
- **Couverture** : `tests/integration/test_escalation_config_contract.py` (tier 2, 2 tests) :
  - `test_post_existing_position_returns_409_and_does_not_overwrite`
  - `test_post_new_position_still_succeeds` (garde-fou sur-fix)

### INV-020 [M] ✅ Chaîne d'escalade : user_id uniques
Un même user ne peut pas être à 2 positions.
- **Pourquoi** : éviter de sonner 2 fois le même user. La logique pure `_find_next_user_id` (`backend/app/logic/escalation.py`) s'appuie sur cette unicité — sans elle, certaines mutations équivalentes deviennent observables (cf pragmas dans le code).
- **Fix** : enforced sur les 2 endpoints d'écriture :
  - `POST /api/config/escalation/bulk` : check `len(user_ids) != len(set(user_ids))` → 422 (pré-existant).
  - `POST /api/config/escalation` (single insert) : check user_id déjà dans la chaîne → 409 (ajouté 2026-04-25).
- **Couverture** : `tests/integration/test_escalation_config_contract.py` :
  - `test_post_existing_user_id_returns_409_and_does_not_overwrite` (INV-020)

---

## 3. Acquittement

### INV-030 [H] ✅ Ack enregistre qui et quand
POST /ack → `acknowledged_at = now`, `acknowledged_by = user_id`, `acknowledged_by_name = user.name`.
- **Pourquoi** : audit trail.
- **Couverture** : E2E `TestAlarmAcknowledgement::test_acknowledge_stores_user_name`.

### INV-031 [C] ✅ Seuls les users notifiés peuvent acquitter
POST /alarms/{id}/ack réussit UNIQUEMENT si `current_user.id IN alarm.notified_user_ids`. Sinon → 403.
- **Pourquoi** : éviter les acquittements par erreur (admin qui ne voit pas l'alarme, user qui tape la mauvaise URL, etc.).
- **Fix** : PR5 (cc35b7d). Vérification ajoutée dans `alarms.py::acknowledge_alarm` via `evaluate_ack_authorization`.
- **Couverture** :
  - Unit : `test_ack_authorization.py` (7 tests : allowed/denied, liste vide, cumulative, pureté)
  - E2E : TOUS les tests ACK existants utilisent un user notifié (vérification implicite). `test_alarm_lifecycle_audit_events` a été adapté pour assigner explicitement admin.

### INV-032 [M] ✅ Ack cumulative : tous les notifiés voient l'alarme acquittée
Après ACK par user1, user2 (qui était aussi notifié) voit l'alarme dans `/mine` avec `status = acknowledged`.
- **Pourquoi** : chacun doit savoir qu'on a répondu.
- **Couverture** : E2E `TestAckedAlarmVisibility::test_acked_alarm_visible_to_other_notified_user`.

### INV-033 [L] ✅ ack_remaining_seconds décompte correctement
Le champ calculé `ack_remaining_seconds ≈ (suspended_until - now).total_seconds()`.
- **Pourquoi** : UI countdown.
- **Couverture** : E2E `TestAckedAlarmVisibility::test_ack_remaining_seconds_in_response` (1800 → 1200 après +10min).

---

## 4. Heartbeat et watchdog

### INV-040 [C] ⚠️ Heartbeat met online + timestamp
POST /devices/heartbeat → `user.is_online = True`, `user.last_heartbeat = now`.

### INV-041 [H] ⚠️ Watchdog détecte les offlines
Tous les 30s, si `user.last_heartbeat < now - 60s` ET `is_online=True` → `is_online=False`.
- **Pourquoi** : détection de déconnexion non-annoncée.

<!-- INV-042 déplacé en section 12 (meta / outils de test) -->

### INV-043 [M] ⚠️ Heartbeat sur replica → 503
Un heartbeat envoyé à un noeud replica renvoie 503 (l'app doit failover sur le primary).
- **Pourquoi** : seul le primary écrit.

---

## 5. Astreinte (oncall)

> **Changement de stratégie 2026-05-26** : la création automatique d'une alarme "oncall_offline" (INV-050) générait trop de faux positifs — un opérateur qui perd sa data (4G/Wi-Fi) mais garde son signal cellulaire (SMS + voix) est marqué offline alors qu'il reste joignable. Décision : **supprimer la création d'alarme** sur user pos 1 offline, et la **remplacer par un tracking statistique** des transitions online ↔ offline (cf nouveau INV-056). L'email "personne en ligne" (INV-053) reste, car il couvre un cas business différent (sortie de boucle quand le système ne sait plus à qui parler). Cohérent avec la décision client `INV-ANDROID-302/305` (la sonnerie locale "hors connexion" ne se déclenche que si le téléphone est totalement injoignable — data + cellulaire perdus).

### INV-050 [C] ❌ DEPRECATED (2026-05-26) — l'alarme oncall_offline n'est plus créée
Avant : si user position 1 a `is_online=False` et `last_heartbeat < now - 15min` → création alarme `is_oncall_alarm=True`. **Cette logique est supprimée.**
- **Pourquoi** : trop de faux positifs (cf encadré "Changement de stratégie" en tête de section). Remplacé par tracking statistique (INV-056) — pas d'escalade automatique sur déconnexion data.
- **À faire dans le PR de fix** : supprimer le code de création (`logic/oncall.py::evaluate_oncall_creation_plan` ou équivalent), supprimer les tests `test_oncall.py::TestInv050CreateAlarmAfterDelay` et `TestOnCallDisconnectionAlarm::test_oncall_offline_15min_creates_alarm`, retirer la clé `oncall_offline_delay_minutes` de `SystemConfig` si elle n'a plus d'autre usage (vérifier que INV-053 ne s'en sert pas — voir ci-dessous).
- **Statut historique** : couverture conservée comme témoin jusqu'à la PR de retrait. À ne PAS étendre.

### INV-051 [H] ❌ DEPRECATED (2026-05-26) — corollaire de INV-050
Avant : alarme oncall auto-résolue au retour online de pos 1. **Cette logique disparaît avec INV-050.**
- **À faire dans le PR de fix** : supprimer `logic/oncall.py::auto_resolve_oncall_alarms` et les tests `TestInv051AutoResolveOnReconnect` + `test_oncall_alarm_auto_resolves_on_reconnection`.

### INV-052 [H] ❌ DEPRECATED (2026-05-26) — corollaire de INV-050
Avant : alarme oncall assignée au prochain online dans la chaîne. **Sans alarme à assigner, sans objet.**
- **À faire dans le PR de fix** : supprimer `TestInv052AssignedToNextOnline` ; vérifier que `logic/oncall.py` n'expose plus de helper "find next online".

### INV-053 [C] ✅ Personne en ligne > 15min → email direction technique (déclencheur découplé de l'alarme)
Si **tous les users** sont `is_online=False` et que **la condition dure > 15 min** (mesuré sur `last_heartbeat` du plus récent online connu, ou bien sur le timestamp de l'event "went_offline" du dernier user à passer offline — à préciser dans le PR de refactor) → email à `system_config.alert_email`. **Aucune alarme n'est créée** (ni à l'époque ni maintenant).
- **Pourquoi** : dernier recours business — quand le système n'a plus aucun téléphone joignable, prévenir un humain par email pour qu'il intervienne hors-bande (appel direct, déplacement, etc.).
- **Changement 2026-05-26** : avant, le code d'INV-053 était entrelacé avec la création d'alarme INV-050 (même tick d'évaluation). Avec la suppression de INV-050, INV-053 devient un **chemin autonome** : la boucle de surveillance évalue uniquement la condition "tous offline > 15 min" et envoie l'email, sans flux de création d'alarme. Anti-spam : 1 seul email par épisode (réf INV-085 / quorum / dissensus comme modèle de cooldown), à confirmer dans le PR.
- **Couverture existante à conserver** :
  - Unit : `test_oncall.py::TestInv053EmailIfNobodyOnline::test_nobody_online_sends_email`
  - E2E : `TestOnCallDisconnectionAlarm::test_nobody_connected_sends_email` (Mailhog)
- **Manque à combler dans le PR de refactor** : test qui vérifie qu'INV-053 fonctionne **après** retrait de INV-050 — le hook d'évaluation peut avoir besoin d'un nouveau call site.

### INV-054 [H] ❌ DEPRECATED (2026-05-26) — corollaire de INV-050
Avant : pas de doublon d'alarme oncall. **Sans alarme oncall, sans objet.**
- **À faire dans le PR de fix** : supprimer `TestInv054NoDuplicateOncallAlarm`.

### INV-055 [M] ✅ → REINTERPRÉTÉ 2026-05-26 : seul position 1 déclenche l'email INV-053 ; le tracking INV-056 concerne tous les users
Avant : "seul position 1 déclenche la surveillance" (= la création d'alarme oncall). Avec la suppression de INV-050, cet invariant reste pertinent pour clarifier **qui** déclenche **quoi** :
- L'**email** INV-053 ("tous offline > 15 min") évalue toujours la chaîne et se déclenche dès que la condition est vraie. La position de l'opérateur dans la chaîne n'a pas d'importance pour l'email — c'est la situation "personne joignable" qui compte. (Note : cette reformulation est plus large que la version précédente où l'email ne partait que si pos 1 offline en plus.)
- Le **tracking** INV-056 enregistre les transitions de **tous** les users (pas seulement pos 1) — pour stats RH, monitoring qualité réseau, KPI astreinte. La spec antérieure "seul pos 1 est monitoré" ne s'applique plus.
- **À faire dans le PR de fix** : mettre à jour `TestInv055OnlyPos1IsMonitored` (renommer + adapter au nouveau périmètre, ou supprimer si redondant avec INV-053/056). Décider si l'email doit garder un AND avec "pos 1 offline" ou se déclencher dès "tous offline" — à trancher par le mainteneur.

### INV-056 [H] 🐛 Tracking des transitions online ↔ offline (remplace l'alarme INV-050)
Chaque transition `is_online: True → False` ou `False → True` sur **n'importe quel user** doit être enregistrée comme un event dans une table dédiée (proposition : `connectivity_events` avec colonnes `id`, `user_id`, `event` ∈ {`went_online`, `went_offline`}, `ts`). Le watchdog (INV-041) émet les events `went_offline` au moment où il flip `is_online`. Le POST /devices/heartbeat émet `went_online` au moment où il flip `is_online` (uniquement lors de la transition, pas à chaque heartbeat).
- **Pourquoi** : on a coupé l'alarme automatique (INV-050) qui était trop bruyante. On garde un suivi factuel "qui est passé offline, combien de temps, combien de fois" pour :
  - éclairer les statistiques d'astreinte (disponibilité opérateur, qualité réseau du terrain),
  - permettre au mainteneur d'identifier un opérateur qui a un problème récurrent (téléphone HS, zone mal couverte),
  - garder un historique exploitable a posteriori si un incident révèle qu'un opérateur était hors-réseau.
- **Statut 🐛** : règle introduite 2026-05-26 ; table + emission d'events à créer (migration DB + hooks dans `watchdog.py` et `api/heartbeat`).
- **Exposition API** (minimum viable) :
  - `GET /api/users/{user_id}/connectivity-history?days=<N>` : retourne la liste paginée des events, du plus récent au plus ancien.
  - `GET /api/stats/connectivity?weeks=<N>` : agrégat par user (nombre de transitions offline, durée cumulée offline, % uptime sur la fenêtre).
  - Auth : tokens admin uniquement (les opérateurs n'ont pas besoin de voir l'historique des autres).
- **Frontend** (hors scope minimum, à prévoir en follow-up) : onglet "Disponibilité opérateurs" dans l'admin web, avec courbe temporelle par user.
- **Anti-pollution** : ne PAS enregistrer un event si la transition est intra-tick du watchdog (offline détecté puis online ré-établi dans le même appel — peu probable mais filtrer). Et ne PAS double-poster un event si l'app envoie un heartbeat alors qu'elle était déjà `is_online=True`.
- **Couverture à créer** :
  - Unit `test_connectivity_events.py::test_went_offline_inserted_when_watchdog_flips_user` : forcer un user `is_online=True, last_heartbeat=now-90s` → tick watchdog → assert ligne `went_offline` dans `connectivity_events`.
  - Unit `test_went_online_inserted_only_on_transition` : heartbeat sur user `is_online=False` insère `went_online` ; heartbeat sur user déjà `is_online=True` n'insère rien.
  - Integration `test_connectivity_history_api` : créer 3 transitions, GET endpoint → vérifier ordre + pagination.
  - E2E (tier 3) : `test_full_offline_cycle_recorded` : login → 60 s sans heartbeat → re-heartbeat → vérifier 2 events en DB.
- **Rétention** : à trancher. Par défaut : garder 365 jours (1 an), purger via tâche planifiée. À aligner avec les autres tables temporelles (`alarms`, `audit_log`).

---

## 6. SMS et Calls

### INV-060 [H] ✅ SMS enqueued après `sms_call_delay_minutes` sur notification
Pour chaque `AlarmNotification`, si `(now - notified_at) / 60 >= sms_call_delay` ET `sms_sent=False` → insert dans `SmsQueue`, set `sms_sent=True`.
- **Pourquoi** : contacter par SMS uniquement si FCM n'a pas réveillé l'user.
- **Fix/Extraction** : PR3 (0716174). Logique dans `logic/sms_timer.py`.
- **Couverture** :
  - Unit : `test_sms_call_timers.py::TestBasicCases` (6 tests : empty, below/exactly/above delay, notified_at None)
  - E2E : `TestSmsAndHealth::test_sms_written_to_queue_on_escalation`

### INV-061 [H] ⚠️ Pas de SMS si phone_number NULL
User sans `phone_number` → skip SMS (idem Call).
- **Pourquoi** : pas de destination.
- **Note** : géré par l'appelant (`_enqueue_sms_for_user` dans `escalation.py`), pas par la fonction pure (qui ne voit pas le phone_number). Couvert implicitement par les E2E SMS.

### INV-062 [H] ✅ Anti-doublon SMS
Pas de SMS identique (`to_number`, `body`) en état `sent_at=NULL, retries<3` à tout instant.
- **Pourquoi** : éviter spam du user.
- **Couverture** :
  - Unit : `test_sms_call_timers.py::TestAntiDuplicate::test_sms_already_sent_not_re_enqueued` (via flag `sms_sent=True`)
  - E2E : `TestRedundancy::test_no_duplicate_sms_from_escalation` (cluster)

### INV-063 [H] ✅ Anti-doublon Call
Pas de Call identique (`to_number`, `alarm_id`) en état `called_at=NULL, retries<3`.
- **Couverture** : Unit `test_sms_call_timers.py::TestAntiDuplicate::test_call_already_sent_not_re_enqueued`.

### INV-064 [M] ⚠️ SMS/Call exclu après 3 retries
SMS/Call avec `retries >= 3` n'apparaît plus dans `/internal/sms|calls/pending`.
- **Pourquoi** : éviter retry infini sur numéro invalide.
- **Couverture** : E2E `TestSmsAndHealth::test_sms_excluded_after_max_retries`.

### INV-065 [C] ⚠️ Gateway key obligatoire
`/internal/sms/*` et `/internal/calls/*` nécessitent header `X-Gateway-Key` valide.
- **Pourquoi** : sécurité — ces endpoints exposent les numéros de tel.
- **Couverture** : E2E `TestSmsAndHealth::test_sms_pending_requires_gateway_key` + `test_sms_pending_wrong_key_returns_401`.

### INV-066 [H] ✅ Toutes les références temporelles utilisent `clock_now()`, pas `datetime.utcnow()`
**Contexte** : le backend a une horloge injectable (`clock.py`) pour que les tests puissent simuler "15 minutes se sont écoulées" en appelant `POST /api/test/advance-clock?minutes=15` au lieu d'attendre réellement 15 minutes. `clock_now()` retourne `datetime.utcnow() + offset`. En prod, l'offset est à 0, donc `clock_now() == datetime.utcnow()`.

**Invariant** : toute écriture de timestamp (`notified_at`, `created_at`, `last_heartbeat`, `acknowledged_at`, `suspended_until`, etc.) doit utiliser `clock_now()`. Sinon, en test, une partie des timestamps est "temps simulé" et une autre est "temps réel" → incohérences.

**Fix** : PR3 (0716174). 3 call sites `notified_at` corrigés :
- `alarms.py::_add_notified_user` : ajout `notified_at=clock_now()`
- `test_api.py::send_test_alarm` (POST `/api/test/send-alarm`) : idem
- `test_api.py::trigger_escalation` (POST `/api/test/trigger-escalation`) : idem

**Couverture** : l'invariant est maintenant respecté dans tous les call sites. Pas de test unit dédié (la fonction pure ne voit que des snapshots avec dates déjà calculées), mais les tests E2E qui combinent advance_clock + création d'alarme valident implicitement.

**Écart latent connu (audit 2026-04-20)** : `send_alarm` dans `alarms.py:84-89` ne passe pas `created_at=clock_now()` explicitement ; l'objet `Alarm` récupère sa valeur via le default SQLAlchemy `datetime.utcnow` (`models.py:38`). Non problématique tant que les tests créent l'alarme **avant** tout `advance-clock`, mais un futur test qui ferait `advance-clock` puis `POST /alarms/send` verrait un `created_at` en « temps réel ». À forcer en `clock_now()` lors de l'implémentation d'INV-018 (ce sera le bon moment, puisqu'on touchera au modèle `Alarm`).

---

## 7. Authentification et sécurité

### INV-070 [C] ⚠️ Login case-insensitive
POST /auth/login avec "ADMIN", "Admin", "admin" → tous acceptés (si password OK).

### INV-071 [M] ⚠️ Nom sans espace
POST /auth/register avec "bad name" → 422 ou 400.
- **Pourquoi** : ergonomie (pas de quoting dans les URLs, etc.).

### INV-072 [M] ⚠️ Nom stocké en lowercase
POST /auth/register avec "TestUser" → stocké "testuser".

### INV-073 [H] ✅ Rate limiting login
Plus de 10 tentatives échouées en 60s pour le même username → 429.
- **Pourquoi** : protection brute-force.
- **Code** : `backend/app/api/users.py:18-32` (`_check_rate_limit`, `RATE_LIMIT_MAX_FAILURES = 10`, `RATE_LIMIT_WINDOW = 60`), appelé ligne 62. Raise 429 au 11e échec.
- **Couverture** (audit 2026-04-20) : E2E `tests/test_improvements.py::TestRateLimiting` — `test_login_rate_limited_after_many_failures` + `test_legitimate_login_still_works_after_rate_limit`.

### INV-074 [C] ✅ Refresh token produit un nouveau token
POST /auth/refresh avec token valide → nouveau token différent.
- **Pourquoi** : un refresh qui renverrait le token reçu en input ne sert à rien (pas de rotation TTL).
- **Implémentation** : `create_access_token` (auth.py:28-37) inclut `"jti": str(uuid.uuid4())` dans le payload → chaque token a un UUID unique → tokens trivialement distincts à chaque appel, sans dépendre du timing `iat`.
- **Fix** : PR #92 (bot IA, 2026-05-13). Aucune modif prod ; verrouillage en régression via 2 tests.
- **Couverture** : `tests/integration/test_auth_refresh.py` (tier 2) :
  - `test_new_refresh_returns_distinct_token` (token_after != token_before)
  - `test_new_token_is_usable` (le nouveau token authentifie GET /api/auth/me)
- **Limites identifiées (issues follow-up)** :
  - #96 [H] : path négatif (`/auth/refresh` avec token expiré ou signature invalide → 401)
  - #97 [human-required] : clarifier que l'ancien token reste valide jusqu'à `exp` naturelle (JWT stateless, pas de denylist — choix architectural assumé)

### INV-075 [C] ⚠️ Token cross-node
Un token émis par un noeud est accepté par tous (même SECRET_KEY).

### INV-076 [C] ✅ ENABLE_TEST_ENDPOINTS=false → /api/test/* renvoie 404
En production, tous les endpoints de test sont désactivés.
- **Implémentation** : `_require_test_endpoints()` ([backend/app/api/test_api.py:18](backend/app/api/test_api.py:18)) appelé en tête de chaque handler `/api/test/*` (22 handlers couverts au 2026-05-29). Le flag `ENABLE_TEST_ENDPOINTS` est lu au module-level depuis env var, défaut `"false"`.
- **Couverture CI** : nouveau job `prod_config_check` dans `.github/workflows/pr.yml` qui lance `pytest tests/prod_config` dans un process **séparé** de tier 2 (la conftest tier 2 force `=true`, la conftest prod_config force `=false` — mixer dans le même run = non-déterministe).
- **Tests** (`tests/prod_config/test_test_endpoints_disabled_inv076.py`) :
  - 6 endpoints critiques paramétrés (reset, send-alarm, advance-clock, simulate-watchdog-failure, status, trigger-escalation) → 404 attendu
  - Défense en profondeur : même avec JWT admin valide, 404 attendu (guard avant `Depends(get_current_admin)`)
  - Anti-faux-positif : `/api/config/escalation` et `/api/cluster` restent fonctionnels (le flag ne casse QUE `/api/test/*`)

### INV-077 [H] ✅ Admin-only endpoints protégés
DELETE /users/{id}, POST /alarms/reset, POST /config/* → requièrent `is_admin=True`.
- **Test** : user non-admin → 403.
- **Fix** : PR #93 (bot IA, 2026-05-13). Aucune modif prod (tous les handlers déclarent déjà `Depends(get_current_admin)`). Sensibilité prouvée par mutation manuelle (`get_current_admin` → `get_current_user` → test RED).
- **Couverture** : `tests/integration/test_admin_only_endpoints.py` (tier 2, 1 test paramétré × 5 endpoints) :
  - `DELETE /api/users/{id}`, `POST /api/alarms/reset`, `POST /api/config/escalation`, `POST /api/config/escalation/bulk`, `POST /api/config/system`
  - Stratégie : body pydantiquement valide pour éviter faux positif 422 ; body non-destructif côté admin (id inexistant, payload conflit)
  - Pour chaque endpoint : assert user1 → 403, assert admin → non-403

### INV-078 [M] ✅ Logout supprime le token FCM
POST /devices/fcm-token DELETE → retire de la base, plus de push reçu.
- **Fix** : PR #95 (bot IA, 2026-05-13). Aucune modif prod (`delete_fcm_token` dans `devices.py:126-139` exécute déjà `db.query(DeviceToken).filter(...).delete() + commit`).
- **Couverture** : `tests/integration/test_fcm_logout.py` (tier 2, 2 tests) :
  - `test_delete_fcm_token_removes_db_row_inv078` (cœur : présence avant / absence après, vérification DB directe indépendante du chemin API)
  - `test_delete_fcm_token_only_removes_target_device_inv078` (défense en profondeur : suppression scopée au `device_id`, pas de wipe massif)
- **Note méthodo** : le 1er run bot avait abandonné (P5 strict sur cas ambigu — le body issue ne disait pas explicitement "verrouille en régression"). Issue #72 reformulée + PR #94 (`.github/ai-bot/prompt.md`) ajoute la section "Cas special : code deja conforme (regression lock)" pour éviter cette classe de faux-abandons sur le reste du backlog.

---

## 8. Chaîne d'escalade et config

### INV-080 [C] ✅ Chaîne vide + alarme envoyée → email
POST /alarms/send avec chaîne vide → email direction technique, alarme persiste avec fallback user.
- **Pourquoi** : ne pas perdre l'événement.
- **Fix/Extraction** : PR5 (cc35b7d). Logique dans `logic/alarm_creation.py`.
- **Couverture** :
  - Unit : `test_alarm_creation.py::TestInv080ChainEmpty` (3 tests : fallback 1er user, pas d'user, override explicite + email)
  - E2E : `TestEmptyEscalationChainAlert::test_alarm_with_empty_chain_sends_email` + `TestEmailViaMailhog::test_empty_chain_sends_real_email`

### INV-081 [M] ⚠️ /config/escalation/bulk remplace + notifie
POST /config/escalation/bulk → DELETE + INSERT de la chaîne + FCM push à tous les users affectés.
- **Couverture** : E2E `TestEscalationChainBulk::test_save_escalation_chain_replaces_all`.

### INV-082 [H] ✅ /config/escalation/bulk est atomique (transaction unique)
`POST /api/config/escalation/bulk` modifie la chaîne en supprimant TOUTES les règles existantes puis en insérant les nouvelles. Si cette opération n'était pas dans une seule transaction, il existerait une **fenêtre de temps (~ms)** pendant laquelle la chaîne serait VIDE en base.

**Pourquoi c'est grave** : pendant cette fenêtre, si une alarme arrive (`POST /alarms/send`), le code verrait `chaîne vide` → déclencherait INV-080 (email direction technique + fallback user). L'alarme serait envoyée à la mauvaise personne, et un email inutile serait envoyé, juste parce qu'un admin modifiait la chaîne au mauvais moment.

**Invariant** : DELETE + INSERT doivent être dans une transaction SQL unique. Une lecture (GET /config/escalation) concurrente ne doit jamais voir 0 lignes tant que la précédente chaîne était non-vide.

**État du code** : **l'invariant est respecté**. `SessionLocal` est configurée `autocommit=False, autoflush=False` (`backend/app/database.py:15`). Le handler `save_escalation_chain_bulk` (`backend/app/api/config.py:120-144`) exécute DELETE puis INSERTs puis un seul `db.commit()` — donc une unique transaction. PostgreSQL READ COMMITTED garantit qu'aucune session concurrente ne voit la liste vide avant commit.

**Fix** : PR #89 (bot IA, 2026-05-12). Aucune modif prod ; ajout d'un test de race qui **verrouille** la propriété en régression.
- **Couverture** : `tests/integration/test_escalation_config_atomicity.py` (tier 2, 1 test) :
  - `test_bulk_atomic_no_empty_chain_observed_under_concurrent_reads` : 1 writer × 3 readers en threads (30 writes × 200 reads chacun), assertion centrale `empty_observations == []`.
- **Sensibilité prouvée empiriquement** : injection `db.commit()` + sleep 5ms après le DELETE → test fail avec 181 violations détectées ; mutation reverte → test pass.

### INV-083 [H] ⚠️ User supprimé → alarmes actives réassignées
DELETE /users/{id} avec alarmes actives assignées → réassignation au premier de la chaîne (hors user supprimé) AVANT delete.
- **Pourquoi** : éviter FK SET NULL orphelin.

### INV-084 [C] 🐛 Tous les délais métier sont paramétrables via SystemConfig
Aucun délai métier ne doit être hardcodé dans le code. Chaque valeur est lue depuis `SystemConfig` à chaque usage (pas cachée), pour qu'un changement admin prenne effet immédiat.

**Inventaire des délais (source de vérité — mis à jour post PR5)** :

| Clé SystemConfig | Défaut | Usage | Statut actuel |
|---|---|---|---|
| `escalation_delay_minutes` | 15 | Délai entre chaque palier d'escalade (INV-011) | ✅ lu en DB (pure fn en prend la valeur en paramètre) |
| `sms_call_delay_minutes` | 2 | Délai avant enqueue SMS/Call après notification (INV-060) | ✅ lu en DB |
| `oncall_offline_delay_minutes` | 15 | ~~Délai avant qu'oncall offline déclenche alarme (INV-050)~~ → INV-050 DEPRECATED 2026-05-26. Usage résiduel = délai de l'email INV-053 si le mainteneur garde la même fenêtre 15 min. À retirer du seed sinon. | ⚠️ à clarifier dans le PR de retrait INV-050 |
| `watchdog_timeout_seconds` | 60 | Délai avant marquer user offline (INV-041) | 🐛 seedé mais **hardcodé** dans `watchdog.py:14` |
| `escalation_tick_seconds` | 10 | Période de la boucle d'escalade | 🐛 **hardcodé** dans `escalation.py:331` (+ `watchdog.py:48` avec 30s — voir note ci-dessous) |
| ~~`fcm_escalation_delay_minutes`~~ | — | **Retiré**. Plus lu (PR2, INV-011) ni seedé (vérifié 2026-04-20). Rien à faire. | ✅ |
| ~~`ack_suspension_minutes`~~ | — | **Non paramétrable** (décision IMPROVEMENTS #13) — 30 min hardcodé voulu | ✅ retiré du seed en PR0 |

**Note audit 2026-04-20** : `watchdog.py:48` utilise `asyncio.sleep(30)` hors tableau ci-dessus. Quand on migrera `escalation_tick_seconds` en `SystemConfig`, décider si le watchdog partage la clé (probable : oui, simplifie) ou a la sienne (`watchdog_tick_seconds`). À trancher au moment du fix.

**Décision 2026-05-12 (issue #75)** : **2 clés séparées** — `escalation_tick_seconds` (default 10) lue par `escalation.py`, `watchdog_tick_seconds` (default 30) lue par `watchdog.py`. Raisons : sémantique propre ("fréquence évaluation escalade" ≠ "fréquence check offline"), 2 leviers indépendants pour accélérer les tests, coût marginal négligeable.

**Pourquoi paramétrer les ticks** (`escalation_tick_seconds`, `watchdog_timeout_seconds`) :
- Non seulement pour la flexibilité admin, mais surtout pour **accélérer les tests**. En test, `escalation_tick_seconds=1` réduit le temps d'attente de 10s à 1s par tick. Gain énorme sur une suite de 66 min.
- En prod, 10s reste la valeur par défaut.

**Règle d'écriture** : toute nouvelle constante temporelle doit être dans SystemConfig dès le jour 1. Pas de "TODO paramétrer plus tard".

### INV-085 [C] ✅ Perte de quorum cluster → email direction technique
Si le cluster perd son quorum (< majorité de noeuds healthy dans Patroni/etcd), un email est envoyé à `system_config.alert_email` pour alerter la direction technique qu'une intervention manuelle est requise.
- **Pourquoi** : sans quorum, aucun primary ne peut être élu → écritures bloquées → alarmes non traitées. Le système ne peut pas se récupérer tout seul.
- **Conditions de déclenchement** :
  - `quorum.has_quorum == False` dans `/api/cluster` (moins de N/2+1 noeuds healthy)
  - OU Patroni injoignable depuis tous les noeuds pendant > **3 minutes** (seuil arbitré 2026-04-20 pour couvrir les glitches courts type redémarrage Patroni sans flapping)
- **Anti-spam** : 1 email initial + reminders à **1h, 3h, 6h** jusqu'à résolution (arbitré 2026-04-20). Équilibre information opérateur ↔ bruit. Le reminder s'arrête dès que `has_quorum == True` à nouveau.
- **Architecture** : `quorum_monitor_loop()` (tick 60s, leader-gated via `is_leader.is_set()` pour éviter 3 emails en cluster) construit un snapshot `/api/cluster` à chaque tick, garde un historique in-memory > 3 min, appelle `_run_quorum_check` qui orchestre `evaluate_quorum_loss` + `should_send_initial_email` + `should_send_reminder`. État persistant dans `quorum_state` (singleton id=1) : `lost_since`, `email_sent_at`, `reminders_sent_at` (JSON liste secondes), reset complet au retour à sain.
- **Tests** :
  - Tier 1 unit pur (`tests/unit/test_quorum_detection.py`) : 21 tests (détection + 2 fonctions pures, boundaries `>= 1h/3h/6h` mutmut-aware)
  - Tier 2 intégration (`tests/integration/test_quorum_monitor_inv085.py`) : 4 tests (envoi initial + reset + reminder 1h + anti-doublon 60s)
  - Tier 3 smoke manuel : stopper 2 des 3 etcd → quorum perdu → email arrive dans Mailhog dans les 4-5 min (3 min anti-flapping + 1 tick).

---

## 9. Cluster et haute disponibilité

### INV-090 [C] ⚠️ Exactement 1 primary
À tout instant, `sum(role == 'primary' for noeud in cluster) == 1`.

### INV-091 [C] ⚠️ Failover en < 60s
Primary stop → nouveau primary élu (Patroni + etcd) en moins de 60s.

### INV-092 [H] ⚠️ Données visibles cross-noeud
Alarme créée sur n1 → visible sur n2 immédiatement (ou <1s de replication lag).

### INV-093 [H] ❌ Pas de split-brain
Aucun scénario (partition réseau, Patroni bug) ne doit produire 2 primaries.
- **Test** : simuler partition avec toxiproxy. Non couvert aujourd'hui.

### INV-094 [M] ⚠️ Persistance après restart
Docker restart → alarmes et users conservés (volume pgdata).

### INV-095 [H] ❌ Écriture atomique au failover
Si primary crash pendant `POST /alarms/send` → soit l'alarme est commitée (visible partout), soit pas (pas de demi-alarme). Pas d'état intermédiaire observable.
- **🐛 Non testé**.

---

## 10. Observabilité et audit

### INV-100 [M] ⚠️ Chaque action critique → AuditEvent
Création, escalade, ack, résolution, modification config, login → entrée dans `audit_events`.

### INV-101 [M] ⚠️ correlation_id propagé
Chaque requête a un `X-Correlation-ID` (header), tracé dans les logs et AuditEvent.

### INV-102 [H] ✅ /health retourne 503 si boucle d'escalade stale
Si `last_tick_at < now - 120s` → `/health` renvoie 503 avec `escalation_loop: false`.
- **Pourquoi** : monitoring externe doit détecter le blocage.
- **Fix** : PR0 (a8e1e66). Flag `_simulate_stall` dans `escalation.py` qui bloque la mise à jour de `last_tick_at` pendant le test → plus de race avec le tick concurrent. Endpoint `/test/clear-loop-stall` ajouté pour le teardown propre.
- **Couverture** : E2E `TestSmsAndHealth::test_health_endpoint_returns_503_if_loop_stalled` (5 runs consécutifs passés).

---

## 11. Stats et KPI

### INV-110 [L] ✅ days param borné
`GET /alarms/?days=N` → N clampé à [1, 90].
- **Implémentation** : `backend/app/api/alarms.py:123` (`days = max(1, min(days, 90))`).
- **Fix** : PR #91 (bot IA, 2026-05-13). Aucune modif prod ; ajout d'un test paramétré de verrouillage en régression.
- **Couverture** : `tests/integration/test_alarms_days_clamp.py` (tier 2, 1 test paramétré × 4 cases) :
  - `days=0`, `days=-5` → clampé à 1, alarme fraîche visible
  - `days=10` → in-range, alarme fraîche visible (contrôle)
  - `days=10000` → clampé à 90, alarme âgée de 100j (via `advance-clock`) exclue
- **Limites connues** : ne teste pas précisément la borne 90 exacte (un mutant `min(days, 30)` survivrait). Exploite l'asymétrie INV-066 (`datetime.utcnow` vs `clock_now()`) qui sera cassée au merge d'INV-018 — à revoir en lot avec INV-018.

### INV-111 [M] ❌ Filtre "hors heures France"
Exclut weekday 8-12 et 14-17 (heure locale Europe/Paris). Inclut WE et fériés.
- **Pourquoi** : analyse des alarmes hors-astreinte.
- **Test** : peu couvert.

<!-- INV-112 supprimé : le groupement KPI des top récurrentes n'est pas basé sur title exact. À reformuler si un invariant business existe ici. -->


---

## 13. Déclenchement hardware (contact sec on-site)

### INV-120 [H] ✅ Déclenchement gateway level-based, reconciliation backend
Chaque gateway on-site (1 par nœud, identifiée par `gateway_id`) poll son
contact sec NC à intervalle régulier (`DRY_CONTACT_POLL_SECONDS`, défaut 5 s)
et POST `/internal/alarms/report-state` (auth `X-Gateway-Key`) avec son
état observé. Le backend **reconcilie** ensuite l'état "alarme gateway
active" avec les états reportés par les gateways alive selon la
politique d'agrégation OR (cf INV-122).

**Endpoint** : `POST /internal/alarms/report-state`

Body : `{"gateway_id": "<string>", "state": "open" | "closed"}`
- `state="open"` = contact NC ouvert = condition d'alarme physique.
- `state="closed"` = contact NC fermé = état au repos.

Réponse : `{"alarm_active": bool, "dissensus": bool}` (debug gateway).

**Logique backend** (à chaque POST) :
1. **UPSERT** dans table `gateway_states` (`gateway_id` PK, `state`, `last_seen=clock_now()`).
2. **Compute** : `alive_gateways = { g | g.last_seen > clock_now() - liveness_window }` ;
   `should_be_active = any(g.state == "open" for g in alive_gateways)`.
3. **Reconcile** (uniquement sur les alarmes `source = "gateway_dry_contact"`) :
   - `should_be_active=True` ET pas d'alarme `source="gateway_dry_contact"` active
     ET pas d'autre alarme active (INV-001) → **CREATE** alarme `source="gateway_dry_contact"`,
     effet identique à `POST /api/alarms/send` : assignation pos 1 chaîne via
     `evaluate_alarm_creation_plan`, INV-080 chaîne vide, FCM, audit log,
     `original_created_at = clock_now()` (INV-018).
   - `should_be_active=True` ET autre alarme active (`source != "gateway_dry_contact"`,
     ex: `"api"` ou `"oncall"`) → **no-op** (INV-001 : 1 alarme suffit, le user est
     déjà notifié, on n'écrase pas l'autre source).
   - `should_be_active=False` ET alarme `source="gateway_dry_contact"` active →
     **RESOLVE** (`status="resolved"`, `updated_at=clock_now()`, audit log).
   - `should_be_active=False` ET autre alarme active → **no-op** (ne résout PAS
     une alarme manuelle ou oncall).
   - Sinon (`alive_gateways` vide, aucun report récent) → **no-op** (conservateur :
     fail-to-alarm — on ne résout PAS une alarme existante sur silence prolongé).
4. **Dissensus** : cf INV-123.

**Recovery automatique** : aucun état local côté gateway (pas de cache, pas
de queue, pas de mémorisation d'id alarme). Un reboot, un crash ou un
restart de la gateway est sans conséquence — le prochain poll reconcilie
l'état physique avec le backend.

**Pourquoi** :
- Canal de déclenchement physique indépendant du backend des capteurs IT
  amont (capteur fuite/fumée/intrusion/sortie GTC).
- Level-based (vs edge V1) → recovery boot gratuite, pas de race condition,
  code gateway ~30 lignes (vs 200+ pour edge+queue+boot recovery).
- L'aller-retour reconciliation supplante INV-121 (auto-resolve sur retour
  repos) qui était prévu mais jamais introduit dans le catalogue —
  subsumé ici, ne pas créer.

**Auth** : `X-Gateway-Key` obligatoire (cf INV-065). Sans la clé → 401,
pas d'effet de bord (pas d'upsert dans `gateway_states`).

**Migration** : voir bloc "Migrations DB" en bas de section.

**Couverture** : `tests/integration/test_report_state_inv120v2.py` (7 tests
tier 2, tous GREEN — 12 passed avec les 5 tests INV-018 adaptés) :
- `test_report_state_without_key_returns_401` (auth, INV-065)
- `test_report_open_creates_alarm_when_none_active` (CREATE)
- `test_report_open_no_op_when_gateway_alarm_already_active` (idempotence poll)
- `test_report_closed_resolves_gateway_alarm` (RESOLVE)
- (cf INV-122 + INV-123 ci-dessous pour les 3 autres tests)

Couverture indirecte (5e call site INV-018) : `tests/integration/test_alarm_
original_created_at_inv018.py::test_inv018_gateway_report_state_alarm_sets_
original_created_at` (adapté du V1, vérifie que `_create_gateway_alarm` fige
bien `original_created_at` au moment du `POST /report-state`).

**Câblage hardware retenu** (inchangé V1, validé empiriquement 2026-05-13
sur node2) :
- Mode GPIO sur HAT Waveshare SIM7600X, pin GPIO 43 du module
  (silkscreen "IO43"). Câblage `3V3 ─── [contact NC] ─── IO43`, zéro
  composant externe. Repos NC fermé → lit 1. Alarme NC ouvert → lit 0.
- Côté gateway, lecture brute via `AT+CGGETV=43`, mapping `value == 1`
  → `"closed"`, `value == 0` → `"open"`.
- Mode ADC abandonné : maintenir ADC1 à GND >5 s freeze le firmware
  modem (recovery uniquement via PWRKEY). GPIO 43 supporte les 2 états
  sans corruption.

### INV-122 [H] ✅ Redondance hardware multi-gateway, agrégation OR fail-to-alarm
Deux (ou plus) gateways onsite reçoivent le **même contact NC physiquement
splitté** sur leurs IO43 respectifs. Le backend agrège leurs `state`
reportés selon la politique **OR fail-to-alarm** :
**au moins une** gateway alive reportant `"open"` → alarme active.

**Définition "alive"** : `g.last_seen > clock_now() - liveness_window`,
où `liveness_window = 3 × DRY_CONTACT_POLL_SECONDS` côté backend (défaut
15 s pour un poll gateway de 5 s, configurable via env
`GATEWAY_LIVENESS_WINDOW_SECONDS`). Une gateway silencieuse au-delà est
ignorée dans l'agrégation OR (pas d'influence sur la décision).

**Tolérance pannes** :
- 1 gateway crash/reboot → l'autre continue de reporter, alarme toujours
  détectée. Recovery au boot automatique (cf INV-120).
- Toutes silencieuses simultanément (`alive_gateways == ∅`) → no-op
  (ne crée pas, ne résout pas).
- Si une gateway nouvelle (jamais vue) POST `report-state`, simple upsert
  dans `gateway_states` (auto-enrôlement). La protection est l'auth
  `X-Gateway-Key` commune (INV-065), pas une whitelist d'IDs.

**Pourquoi** : la mission #1 du système est de ne JAMAIS rater une alarme
physique. Une seule carte (V1) = point de défaillance unique. Deux
cartes en OR éliminent ce SPOF sans complexité côté backend (la logique
de reconcile est identique pour N=1 ou N=10 gateways).

**Couverture** : `tests/integration/test_report_state_inv120v2.py::
test_or_policy_one_open_one_closed_keeps_alarm_active` (2 gateways alive,
1 open + 1 closed → alarme reste active sur la seule "open"). Cas
"1 gateway silencieuse" non explicitement testé en tier 2 (le test
implicite est : à chaque POST le code recalcule `alive_gateways` via
`last_seen > now - liveness_window`, et la fonction est triviale —
mutation aurait peu de portée). À ajouter en E2E hardware (smoke
PR2/déploiement) en débranchant un fil onsite.

### INV-123 [H] ✅ Détection dissensus capteurs HW + alerte sysadmin
Si parmi les `alive_gateways` (cf INV-122), des gateways reportent des
`state` divergents (au moins une `"open"` ET au moins une `"closed"`)
pendant plus de **5 minutes**, le système alerte qu'un câblage HW est
probablement cassé.

**Champs sur `Alarm`** (uniquement pour les alarmes
`source = "gateway_dry_contact"`) :
- `sensor_dissensus_since: TIMESTAMP NULL` — premier instant où le
  dissensus a été détecté lors de la reconciliation courante. Reset à
  `NULL` dès que les `alive_gateways` redeviennent cohérents (ou alive
  count < 2).
- `sensor_dissensus_email_sent_at: TIMESTAMP NULL` — timestamp du
  dernier email sysadmin émis pour cet épisode. Reset à `NULL` en même
  temps que `sensor_dissensus_since` (anti-spam : 1 seul email par
  épisode, jusqu'à résolution).

**Logique à chaque POST `/internal/alarms/report-state`** (après reconcile,
sur l'alarme `source="gateway_dry_contact"` active si elle existe) :
1. `divergent = (len(alive_gateways) >= 2) ET (set(g.state for g in alive_gateways) == {"open", "closed"})`.
2. Si `divergent` ET `sensor_dissensus_since IS NULL` → set à `clock_now()`.
3. Si `divergent` ET `clock_now() - sensor_dissensus_since > 5 min` ET
   `sensor_dissensus_email_sent_at IS NULL` → envoi email
   `send_alert_email(subject="Discordance capteurs HW — intervention requise",
   body=..., to=<system_config.alert_email>)` ; set
   `sensor_dissensus_email_sent_at = clock_now()`.
4. Si NON divergent (cohérence retrouvée ou alive count < 2) → reset
   les deux champs à `NULL`.

**Flag UI** : si `alarm.sensor_dissensus_since IS NOT NULL`, le frontend
(web admin + Android) affiche un badge orange ⚠ "Discordance capteurs
hardware — vérifier câblage / état des cartes" sur la carte alarme.
Cible : opérateur. Implémentation frontend = PR3 (hors scope PR1).

**Pourquoi** : si une seule carte voit "ouvert" alors que l'autre voit
"fermé", le contact n'est pas câblé correctement (ou une des cartes a un
GPIO mort). Sans alerte, on perd silencieusement la tolérance aux pannes
de INV-122. L'email sysadmin n'est PAS une alerte critique au sens
business (pas un appel téléphonique escaladé) — c'est un info à
direction technique, "merci d'aller voir les cartes". L'alarme métier
sous-jacente (si elle existe via la politique OR) reste active
normalement.

**Couverture** : `tests/integration/test_report_state_inv120v2.py` :
- `test_dissensus_over_5min_sets_flag_and_sends_email` (détection après
  5 min + email avec sujet/contenu différenciés de INV-080 chaîne vide
  + set `sensor_dissensus_email_sent_at`)
- `test_dissensus_resolved_resets_flag_and_no_second_email` (reset des
  2 champs si cohérence retrouvée ; 2e épisode redémarre le compteur,
  pas de 2e email avant 5 min)

Note méthodo : les 2 tests overrident `GATEWAY_LIVENESS_WINDOW_SECONDS=3600`
pour éviter qu'entre re-POST séquentiels d'une gateway après `advance-clock`,
l'autre gateway soit considérée temporairement silencieuse et fasse reset
spurieux du `sensor_dissensus_since`. En prod, chaque gateway poll toutes
les 5s donc les 2 sont en permanence "alive" sur la fenêtre 15s par défaut.

### Migrations DB (PR1)

Inline dans `backend/app/database.py` (pas d'Alembic, cf pattern existant) :

1. **Nouvelle table `gateway_states`** :
   - `gateway_id TEXT PRIMARY KEY`
   - `state TEXT NOT NULL` (valeurs `"open"` ou `"closed"`)
   - `last_seen TIMESTAMP NOT NULL`
   - Idempotent : `CREATE TABLE IF NOT EXISTS` (PostgreSQL) / check
     `sqlite_master` (SQLite).
2. **Nouvelle colonne `alarms.source TEXT NOT NULL DEFAULT 'api'`** :
   - Backfill : `UPDATE alarms SET source = 'oncall' WHERE is_oncall_alarm = TRUE`,
     reste à `'api'` (défaut). Pas d'alarme historique `source="gateway_dry_contact"`
     (le V1 n'écrivait pas ce champ).
   - Valeurs métier : `"api"`, `"oncall"`, `"gateway_dry_contact"`. Stockage
     TEXT (pas enum SQL strict) pour souplesse migration.
3. **Nouvelles colonnes `alarms.sensor_dissensus_since TIMESTAMP NULL`**
   et `alarms.sensor_dissensus_email_sent_at TIMESTAMP NULL` :
   - Toujours NULL au backfill (V1 ne savait pas détecter).

### Suppression de l'endpoint V1 `/internal/alarms/trigger` (PR1)

L'endpoint `POST /internal/alarms/trigger` (V1, mergée PR #100) est
**retiré** en PR1, supplanté par `/internal/alarms/report-state`. Pas
d'alias legacy. Caller production unique : `gateway/modem_gateway.py:660`
(refonte en PR2). PR1 + PR2 doivent être déployés ensemble côté cloud
pour éviter une fenêtre où la gateway encore en V1 reçoive du 404.

Les 5 tests existants `tests/integration/test_dry_contact_trigger.py`
sont **supprimés** en PR1 (ils testent un endpoint qui n'existe plus).
Le test `tests/integration/test_alarm_original_created_at_inv018.py`
ligne 400-469 (5e call site INV-018 via `/internal/alarms/trigger`) est
**adapté** pour pointer sur `/internal/alarms/report-state` avec un body
`{"gateway_id": "test-onsite-1", "state": "open"}`.




---

## 12. Invariants meta (pour les tests)

### INV-900 [C] Isolation des tests
Après `POST /test/reset` + `POST /alarms/reset` + `POST /test/reset-clock`, le système est dans un état déterministe :
- 0 alarmes, tous users online avec heartbeat=now, horloge à offset 0.
- Chaîne d'escalade = [user1, user2, admin].
- `SystemConfig` = valeurs par défaut (15min, 2min, etc.).

### INV-901 [C] Idempotence du reset
Appeler `reset` 2x consécutivement produit le même état que 1 fois.

### INV-902 [H] Un test ne doit jamais dépendre de l'ordre
Avec `pytest-randomly`, tous les tests passent. Si un test dépend d'un état laissé par un autre, il doit EXPLICITEMENT setup cet état.

### INV-903 [L] 🔧 _meta test_ — simulate-connection-loss rejette les heartbeats anciens
`POST /api/test/simulate-connection-loss` rend l'endpoint heartbeat hostile aux tokens émis AVANT l'appel (vérifié via `token.iat < connection_loss_time_int` → 503).
- **Pourquoi** : simuler une perte réseau robuste même quand un émulateur Android continue à envoyer des heartbeats en arrière-plan avec un vieux token.
- **Ce n'est pas un invariant business**, c'est de la plomberie pour les tests d'intégration Android.

---

## Comment ajouter un invariant

1. Identifier la **règle business** (pas l'implémentation).
2. Numéroter avec le prochain ID libre dans la section.
3. Préciser :
    - Description (1 phrase)
    - **Pourquoi** (motivation business, pas technique)
    - **Test hint** (comment le vérifier, pas tout le code du test)
    - Criticité + statut.
4. Si ambigu, créer une section **❓ À clarifier** et demander au propriétaire AVANT d'écrire des tests.
5. Commit : le catalogue fait partie de l'histoire du produit.

## Comment supprimer un invariant

Si le business change (ex: on accepte plusieurs alarmes actives), NE PAS supprimer l'invariant silencieusement. Procédure :
1. PR qui modifie l'invariant avec justification commit.
2. Les tests associés doivent être supprimés DANS LE MÊME PR.
3. Le code doit être modifié DANS LE MÊME PR.

Sinon → tests orphelins qui verrouillent un comportement mort.

---

## ❓ Questions restantes au propriétaire

1. ~~**INV-085 (quorum)** : seuil "Patroni injoignable depuis > X minutes" ?~~ → **Tranché 2026-04-20 : 3 min** (voir INV-085 ci-dessus).
2. ~~**INV-084 fcm_escalation_delay_minutes** : retirer du seed ?~~ → **Classée** : la clé n'est **pas** seedée (vérifié 2026-04-20). Question caduque.
3. ~~**INV-019 (positions uniques)** : tranché 2026-04-20 — rejet 409~~ → **Fixé PR #20 (bot IA lvl 1, 2026-04-21)**.
4. **Suivant** : rédiger `android/INVARIANTS.md` pour les invariants côté client (sonnerie continue, vibration, écran verrouillé, rotation bloquée, reprise post-boot, etc.).

---

## 📊 Backlog des fix prioritaires (post PR 0-5)

Ordre recommandé pour les prochains PRs :

| INV | Criticité | Complexité | Note |
|---|---|---|---|
| INV-018 + INV-018b | C | ⚠️ | ✅ #76 modèle (PR #102 bot, 2026-05-14), ✅ #77 lectures `/alarms` (PRs #114 + #121 bot+Claude), ✅ #78 stats KPI (PR <à venir 2026-05-29> session Claude). **Reste** : #85 frontend `timeAgo` (`human-required`). Backend complet ; impact résiduel = affichage web seul. |
| ~~INV-082~~ | H | — | ✅ Fixé PR #89 (bot IA, 2026-05-12) — test concurrence threading. |
| ~~INV-019~~ | M | — | ✅ Fixé PR #20 (pilote bot IA lvl 1, 2026-04-21). |
| INV-020 | M | ★ | Rejeter user_id dupliqués dans `POST /config/escalation` single endpoint (bulk déjà OK). Distinct de INV-019 qui portait sur position. |
| ~~INV-073~~ | H | — | ✅ déjà fixé et testé (audit 2026-04-20). |
| INV-084 (reste 2/3) | C | ★★ | Migrer `WATCHDOG_TIMEOUT_SECONDS` (issue #74) + `escalation_tick_seconds`+`watchdog_tick_seconds` (issue #75, 2 clés séparées tranché 2026-05-12) en SystemConfig. Sous-cas `ONCALL_OFFLINE_DELAY_MINUTES` fixé PR #25 (2026-04-21). |
| ~~INV-085~~ | C | — | ✅ Fixé via 3 PRs incrémentales : PR #103 (#79 détection pure, 2026-05-18), PR #136 (#80 email initial + #81 reminders 1h/3h/6h + wiring `quorum_monitor_loop` leader-gated + table `quorum_state` singleton, 2026-05-29). Tests : 21 unit + 4 integ. |
| ~~INV-076~~ | C | — | ✅ Fixé via PR #138 (2026-05-29, session Claude) — job CI `prod_config_check` dans `pr.yml` + 8 tests dans `tests/prod_config/`. Issue #82 closes auto au merge. |
| ~~INV-005~~ | H | — | ✅ Fixé PR #27 (pilote bot IA lvl 3, 2026-04-21) — property-based hypothesis. |
| ~~INV-007~~ | M | — | ✅ Fixé PR #90 (bot IA, 2026-05-12) — verrouillage régression sur /active et /mine. |
| ~~INV-110~~ | L | — | ✅ Fixé PR #91 (bot IA, 2026-05-13) — test paramétré clamp days. |
| ~~INV-074~~ | C | — | ✅ Fixé PR #92 (bot IA, 2026-05-13). Follow-ups : issue #96 [H] path négatif, issue #97 clarif stateless JWT. |
| ~~INV-077~~ | H | — | ✅ Fixé PR #93 (bot IA, 2026-05-13) — 5 endpoints admin-only. |
| ~~INV-078~~ | M | — | ✅ Fixé PR #95 (bot IA, 2026-05-13). Cas pédagogique : 1er run abandonné, reformulation + PR #94 prompt.md ajoute section "regression-lock". |
| ~~INV-120 V2 + 122 + 123 backend~~ | H | — | ✅ PR1 (#113, 2026-05-18) `POST /internal/alarms/report-state` + table `gateway_states` + colonnes `Alarm.source` / `sensor_dissensus_*` + suppression `/trigger` V1. ✅ PR2 (#115, 2026-05-18) `DryContactMonitorThread` stateless côté gateway. ✅ Patch (#119, 2026-05-28) ajout `acknowledged` au filtre `any_active_alarm` → no-op pendant ack (bug terrain #118). Reste PR3 : badge UI dissensus web + Android (issue #112 ouverte, scope clarifié dans son commentaire). |

**Parallèles possibles** : PR6 (endpoint `/test/evaluate-now` qui élimine le flaky `trigger-escalation` incomplet) — désirable avant d'attaquer INV-018 car va simplifier les tests.
