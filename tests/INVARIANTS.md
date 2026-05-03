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

## État d'avancement global (mis à jour au 2026-04-20)

**Extraction logique pure** : PR 1-5 terminés (cc35b7d sur master).
- 87 unit tests en 0.14s (logic/ entièrement couvert)
- Suite E2E : 239 passed / 17 skipped / 0 fail en 1h09
- 8 bugs catalogue corrigés : INV-011, INV-015b, INV-031, INV-066, INV-102, INV-073 (audit 2026-04-20) + **INV-019 (PR #20)**, **INV-005 (PR #27)**, **INV-084 oncall_offline (PR #25)** — pilotes bot IA 2026-04-21

**Statut par catégorie** :

| Catégorie | ✅ | ⚠️ | ❌ | 🐛 |
|---|---|---|---|---|
| 1. Alarme lifecycle | 2 | 4 | 0 | 0 |
| 2. Escalade | 6 | 2 | 0 | 3 (INV-018, 018b, 019, 020) |
| 3. Acquittement | 1 | 2 | 0 | 0 (INV-031 ✅ PR5) |
| 4. Heartbeat | 0 | 3 | 0 | 0 |
| 5. Astreinte | 6 | 0 | 0 | 0 (tout ✅ PR4) |
| 6. SMS/Calls | 4 | 2 | 0 | 1 (INV-066 ✅ PR3) |
| 7. Auth | 3 | 3 | 2 | 0 |
| 8. Config/chaîne | 1 | 2 | 0 | 3 (INV-082, 084, 085) |
| 9. Cluster HA | 0 | 3 | 2 | 0 |
| 10. Observabilité | 1 | 2 | 0 | 0 (INV-102 ✅ PR0) |
| 11. Stats | 0 | 2 | 0 | 0 |

**Restant prioritaire** : INV-018 (`original_created_at`), INV-084 (reste 2/3 sous-cas : `watchdog_timeout_seconds` + `escalation_tick_seconds` — sous-cas `oncall_offline_delay_minutes` fixé PR #25), INV-085 (quorum email). INV-082 déjà atomique côté code (il manque juste un test de race — audit 2026-04-20). INV-073 déjà testé (audit 2026-04-20). INV-019 et INV-005 fixés par pilotes bot 2026-04-21.

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

### INV-007 [M] ⚠️ Alarme resolved n'apparaît plus dans /active ni /mine
`GET /api/alarms/active` et `GET /api/alarms/mine` excluent les `status = 'resolved'`.
- **Pourquoi** : UX app mobile.

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

### INV-018b [C] 🐛 Toute lecture historique utilise `original_created_at`
Les usages suivants doivent lire `original_created_at`, PAS `created_at` :
- `alarms.py:116-117` — filtre `/alarms/?days=N` (historique)
- `alarms.py:131` — ORDER BY pour `/active`
- `schemas.py:124` — champ exposé dans `AlarmResponse`
- `stats.py:108, 120, 140, 143` — bucketing KPI par semaine, calcul MTTR
- `index.html:567, 575` — affichage timeAgo dans le front
- **Impact actuel** : une alarme escaladée 2h après création apparaît comme "il y a 2min" dans l'historique, compte dans la mauvaise semaine en KPI, et a un MTTR artificiellement raccourci.
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

### INV-050 [C] ✅ Oncall offline > 15min → alarme auto créée
Si user position 1 a `is_online=False` et `last_heartbeat < now - 15min` → création alarme `is_oncall_alarm=True`.
- **Pourquoi** : la continuité de service DOIT être garantie.
- **Fix/Extraction** : PR4 (878366b). Logique dans `logic/oncall.py`.
- **Couverture** :
  - Unit : `test_oncall.py::TestInv050CreateAlarmAfterDelay` (3 tests : above/exactly/below delay avec boundaries)
  - E2E : `TestOnCallDisconnectionAlarm::test_oncall_offline_15min_creates_alarm`

### INV-051 [H] ✅ Oncall de retour online → alarme oncall auto-résolue
Si une alarme `is_oncall_alarm=True, status in (active, escalated)` existe et position 1 est `is_online=True` → `status → 'resolved'`.
- **Pourquoi** : le problème s'est résolu de lui-même.
- **Couverture** :
  - Unit : `test_oncall.py::TestInv051AutoResolveOnReconnect` (3 tests : resolve, no-op sans alarme, ignore non-oncall)
  - E2E : `TestOnCallDisconnectionAlarm::test_oncall_alarm_auto_resolves_on_reconnection`

### INV-052 [H] ✅ Alarme oncall assignée au SUIVANT, pas au #1
L'alarme oncall n'est PAS assignée à l'user offline (position 1) mais au prochain online dans la chaîne.
- **Pourquoi** : évident (l'offline ne peut pas la prendre).
- **Couverture** :
  - Unit : `test_oncall.py::TestInv052AssignedToNextOnline` (2 tests : next online pos 2, skip offline → pos 3)
  - E2E : `TestOnCallDisconnectionAlarm::test_oncall_offline_15min_creates_alarm` (assert assigned_user_id == user2)

### INV-053 [C] ✅ Personne en ligne → email direction technique
Si tous les users sont offline et position 1 est offline > 15min → email à `system_config.alert_email` (pas d'alarme créée).
- **Pourquoi** : dernier recours, sortir du système.
- **Couverture** :
  - Unit : `test_oncall.py::TestInv053EmailIfNobodyOnline::test_nobody_online_sends_email`
  - E2E : `TestOnCallDisconnectionAlarm::test_nobody_connected_sends_email` (Mailhog)

### INV-054 [H] ✅ Pas de doublon d'alarme oncall
Si une alarme `is_oncall_alarm=True, status in (active, escalated)` existe déjà, ne pas en créer une seconde.
- **Pourquoi** : éviter pollution.
- **Couverture** :
  - Unit : `test_oncall.py::TestInv054NoDuplicateOncallAlarm` (3 tests : active prevents, escalated prevents, resolved allows)

### INV-055 [M] ✅ Oncall : seul position 1 déclenche la surveillance
User position 2+ offline ne déclenche PAS d'alarme oncall, même prolongée.
- **Pourquoi** : seul le #1 a un contrat d'astreinte.
- **Couverture** :
  - Unit : `test_oncall.py::TestInv055OnlyPos1IsMonitored::test_pos2_offline_does_not_trigger_creation`
  - E2E : `TestOnCallDisconnectionAlarm::test_no_oncall_alarm_if_not_position1`

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

### INV-074 [C] ⚠️ Refresh token produit un nouveau token
POST /auth/refresh avec token valide → nouveau token différent.

### INV-075 [C] ⚠️ Token cross-node
Un token émis par un noeud est accepté par tous (même SECRET_KEY).

### INV-076 [C] ❌ ENABLE_TEST_ENDPOINTS=false → /api/test/* renvoie 404
En production, tous les endpoints de test sont désactivés.
- **🐛 Test manquant** : aucun test ne tourne avec cette variable false. En CI, ajouter un job dédié.

### INV-077 [H] ⚠️ Admin-only endpoints protégés
DELETE /users/{id}, POST /alarms/reset, POST /config/* → requièrent `is_admin=True`.
- **Test** : user non-admin → 403.

### INV-078 [M] ⚠️ Logout supprime le token FCM
POST /devices/fcm-token DELETE → retire de la base, plus de push reçu.

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

### INV-082 [H] ⚠️ /config/escalation/bulk est atomique (transaction unique)
`POST /api/config/escalation/bulk` modifie la chaîne en supprimant TOUTES les règles existantes puis en insérant les nouvelles. Si cette opération n'était pas dans une seule transaction, il existerait une **fenêtre de temps (~ms)** pendant laquelle la chaîne serait VIDE en base.

**Pourquoi c'est grave** : pendant cette fenêtre, si une alarme arrive (`POST /alarms/send`), le code verrait `chaîne vide` → déclencherait INV-080 (email direction technique + fallback user). L'alarme serait envoyée à la mauvaise personne, et un email inutile serait envoyé, juste parce qu'un admin modifiait la chaîne au mauvais moment.

**Invariant** : DELETE + INSERT doivent être dans une transaction SQL unique. Une lecture (GET /config/escalation) concurrente ne doit jamais voir 0 lignes tant que la précédente chaîne était non-vide.

**État du code (audit 2026-04-20)** : **l'invariant est déjà respecté**. `SessionLocal` est configurée `autocommit=False, autoflush=False` (`backend/app/database.py:15`). Le handler `save_escalation_chain_bulk` (`backend/app/api/config.py:120-144`) exécute DELETE puis INSERTs puis un seul `db.commit()` — donc une unique transaction. PostgreSQL READ COMMITTED garantit qu'aucune session concurrente ne voit la liste vide avant commit.

**Ce qu'il reste à faire** : écrire le test de race ci-dessous pour **verrouiller** la propriété en régression (candidat pilote bot IA phase 5).

**Test** : thread A fait POST /bulk dans une boucle, thread B fait GET /escalation dans une boucle. B ne doit JAMAIS voir une liste vide (si la chaîne précédente contenait au moins 1 user).

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
| `oncall_offline_delay_minutes` | 15 | Délai avant qu'oncall offline déclenche alarme (INV-050) | ✅ **lu en DB** (PR #25 pilote bot 2026-04-21). Constante renommée en `ONCALL_OFFLINE_DELAY_MINUTES_DEFAULT` (fallback si clé absente), lecture `SystemConfig` à chaque tick d'escalade. |
| `watchdog_timeout_seconds` | 60 | Délai avant marquer user offline (INV-041) | 🐛 seedé mais **hardcodé** dans `watchdog.py:14` |
| `escalation_tick_seconds` | 10 | Période de la boucle d'escalade | 🐛 **hardcodé** dans `escalation.py:331` (+ `watchdog.py:48` avec 30s — voir note ci-dessous) |
| ~~`fcm_escalation_delay_minutes`~~ | — | **Retiré**. Plus lu (PR2, INV-011) ni seedé (vérifié 2026-04-20). Rien à faire. | ✅ |
| ~~`ack_suspension_minutes`~~ | — | **Non paramétrable** (décision IMPROVEMENTS #13) — 30 min hardcodé voulu | ✅ retiré du seed en PR0 |

**Note audit 2026-04-20** : `watchdog.py:48` utilise `asyncio.sleep(30)` hors tableau ci-dessus. Quand on migrera `escalation_tick_seconds` en `SystemConfig`, décider si le watchdog partage la clé (probable : oui, simplifie) ou a la sienne (`watchdog_tick_seconds`). À trancher au moment du fix.

**Pourquoi paramétrer les ticks** (`escalation_tick_seconds`, `watchdog_timeout_seconds`) :
- Non seulement pour la flexibilité admin, mais surtout pour **accélérer les tests**. En test, `escalation_tick_seconds=1` réduit le temps d'attente de 10s à 1s par tick. Gain énorme sur une suite de 66 min.
- En prod, 10s reste la valeur par défaut.

**Règle d'écriture** : toute nouvelle constante temporelle doit être dans SystemConfig dès le jour 1. Pas de "TODO paramétrer plus tard".

### INV-085 [C] 🐛 Perte de quorum cluster → email direction technique
Si le cluster perd son quorum (< majorité de noeuds healthy dans Patroni/etcd), un email est envoyé à `system_config.alert_email` pour alerter la direction technique qu'une intervention manuelle est requise.
- **Pourquoi** : sans quorum, aucun primary ne peut être élu → écritures bloquées → alarmes non traitées. Le système ne peut pas se récupérer tout seul.
- **Conditions de déclenchement** :
  - `quorum.has_quorum == False` dans `/api/cluster` (moins de N/2+1 noeuds healthy)
  - OU Patroni injoignable depuis tous les noeuds pendant > **3 minutes** (seuil arbitré 2026-04-20 pour couvrir les glitches courts type redémarrage Patroni sans flapping)
- **Anti-spam** : 1 email initial + reminders à **1h, 3h, 6h** jusqu'à résolution (arbitré 2026-04-20). Équilibre information opérateur ↔ bruit. Le reminder s'arrête dès que `has_quorum == True` à nouveau.
- **🐛 Non implémenté** : le code actuel expose `/api/cluster` avec `has_quorum` mais ne déclenche aucun email.
- **Test** : stopper 2 des 3 etcd → quorum perdu → email arrive dans Mailhog dans les 2 min.

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

### INV-110 [L] ⚠️ days param borné
`GET /alarms/?days=N` → N clampé à [1, 90].

### INV-111 [M] ❌ Filtre "hors heures France"
Exclut weekday 8-12 et 14-17 (heure locale Europe/Paris). Inclut WE et fériés.
- **Pourquoi** : analyse des alarmes hors-astreinte.
- **Test** : peu couvert.

<!-- INV-112 supprimé : le groupement KPI des top récurrentes n'est pas basé sur title exact. À reformuler si un invariant business existe ici. -->


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
| INV-018 + INV-018b | C | ★★★ | Ajouter `original_created_at` immuable. Migration DB + 5 call sites (stats, schemas, frontend). Gros PR. |
| INV-082 | H | ★ | Code déjà atomique (cf. audit 2026-04-20) — test concurrence `threading.Thread` à ajouter pour verrouiller la propriété. **Candidat pilote bot IA**. |
| ~~INV-019~~ | M | — | ✅ Fixé PR #20 (pilote bot IA lvl 1, 2026-04-21). |
| INV-020 | M | ★ | Rejeter user_id dupliqués dans `POST /config/escalation` single endpoint (bulk déjà OK). Distinct de INV-019 qui portait sur position. |
| ~~INV-073~~ | H | — | ✅ déjà fixé et testé (audit 2026-04-20). |
| INV-084 (reste 2/3) | C | ★★ | Migrer `WATCHDOG_TIMEOUT_SECONDS`, `escalation_tick_seconds` en SystemConfig. Sous-cas `ONCALL_OFFLINE_DELAY_MINUTES` fixé PR #25 (2026-04-21). |
| INV-085 | C | ★★ | Quorum perdu → email. Nécessite un ping Patroni périodique + anti-spam (seuil 3 min + reminders 1h/3h/6h, tranché 2026-04-20). |
| INV-076 | C | ★ | Job CI dédié avec `ENABLE_TEST_ENDPOINTS=false` vérifiant que `/api/test/*` renvoie 404. |
| ~~INV-005~~ | H | — | ✅ Fixé PR #27 (pilote bot IA lvl 3, 2026-04-21) — property-based hypothesis. |

**Parallèles possibles** : PR6 (endpoint `/test/evaluate-now` qui élimine le flaky `trigger-escalation` incomplet) — désirable avant d'attaquer INV-018 car va simplifier les tests.
