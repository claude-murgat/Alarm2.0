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

### INV-001 [C] ✅ Une seule alarme active à la fois **par site**
À tout instant, il existe **au plus une** alarme avec `status IN ('active', 'escalated')` **par valeur de `site`** (INV-155). Les alarmes sans site (alarme d'astreinte, perte de heartbeat) forment leur propre périmètre : au plus une à la fois. Une 2e alarme sur le **même** site est refusée (`409` par l'API ; ignorée par le contact sec, comme aujourd'hui). Une alarme d'**un autre** site est créée normalement.
- **Pourquoi** : design explicite (CLAUDE.md). Évite la surcharge cognitive de l'opérateur.
- **Révision 2026-10-22 (multi-site)** : Izeron et Beaufort seront couverts par la même app et la même équipe de garde. Avec l'ancienne règle globale, une alarme de Beaufort pendant une alarme d'Izeron était **perdue sans bruit** (`alarms_internal.py:273-288`). Choix du propriétaire : une alarme active par site.
- **Couverture** :
  - E2E : `TestSingleAlarm::test_only_one_alarm_at_a_time` (409 sur 2e alarme)
  - Integration `test_multisite_inv155_156.py` : `test_izeron_and_beaufort_alarms_coexist`, `test_second_alarm_same_site_is_409`, `test_dry_contact_beaufort_while_izeron_active`, `test_dry_contact_ignored_when_same_site_active`, `test_oncall_alarm_has_no_site_and_contacts_go_to_default_site` (périmètre sans site)
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

### INV-142 [C] ✅ Escalade : le nouvel assigné reçoit un push au tick même de l'escalade
Quand la boucle d'escalade (`escalation_loop`) passe une alarme à l'utilisateur suivant, ce dernier figure dans `notified_user_ids` **et** reçoit un FCM dans le même tick, avec les anciens notifiés (cumulative, INV-012). Vaut pour tout appelant de `_add_notified_user` (création, escalade de la boucle, escalade par touche 2).
- **Pourquoi** : prod 2026-10-20, alarme 817 : escalade timo → admin à 08:33:31, push envoyé à timo seul (`notified_user_ids: [4]`). Session SQLAlchemy en `autoflush=False` : la notification ajoutée sans `flush` était invisible à la relecture qui choisit les destinataires. admin n'était réveillé qu'au palier suivant ou par le SMS (+2 min). Les tests FCM existants passaient par `/api/test/trigger-escalation` (qui flush) et ne voyaient pas la vraie boucle.
- **Couverture** : Integration `test_escalation_push_new_assignee_inv142.py` (vraie boucle, tick 1 s, horloge avancée). RED avant le correctif : push à l'ancien assigné seul.

### INV-143 [M] ✅ Web : un utilisateur hors garde avec un téléphone s'affiche « En attente », pas « Hors ligne »
L'API expose `has_device` (au moins un jeton FCM enregistré) dans `/api/users/` et dans chaque étape de `/api/alarms/active/escalation`. Le web affiche, partout où il montre un statut (onglet Utilisateurs, Disponibilité, schéma d'escalade) :
- **En ligne** (vert) si `is_online` ;
- **En attente** (orange, « réveillé par push à l'escalade ») si l'utilisateur **n'est pas de garde** (pas en position 1 de la chaîne) et a un téléphone enregistré ;
- **Hors ligne** (rouge) sinon — y compris la personne **de garde** sans heartbeat, qui reste une vraie anomalie.
- **Pourquoi** : les utilisateurs hors garde n'envoient pas de heartbeat par design (INV-ANDROID-600), ils sont réveillés par push. Le web les affichait « Hors ligne » alors que leur téléphone était allumé et joignable ; le propriétaire a cru, le 2026-10-20, qu'ils ne seraient pas appelés.
- **Couverture** : Integration `test_waiting_status_inv143.py` ; Playwright `test_frontend.py::TestWaitingStatus` (joué en local, hors CI).

### INV-144 [C] ✅ Escalade : le SMS et l'appel de l'assigné passent avant l'escalade au suivant
La boucle d'escalade n'escalade une alarme de A vers B qu'une fois l'appel de A **passé** pour cette alarme (`call_queue.called_at` renseigné : sans réponse, occupé, acquitté) **et** le SMS de A **parti** (aucun SMS vers son numéro encore en file : `sent_at` vide et `retries < 3`). Ne concerne que les assignés ayant un `phone_number`. Un SMS ou un appel abandonné après 3 échecs (INV-064) compte comme passé.
- **Appel `error`** (jamais émis, INV-127) : ne compte pas comme passé. `called_at` reste vide, l'appel reste dans `/internal/calls/pending` et la gateway le rejoue (`retries` +1, au plus 3 essais).
- **Révision 2026-10-20** (alarme 853) : SMS de timo en échec (`AT_ERROR`, modem muet), appel en `error` à 13:15:14, compté comme passé → escalade vers admin à 13:15:22 ; le SMS n'est parti qu'à 13:16:03. L'ancienne règle comptait tout `called_at` (même `error`) et ignorait le SMS.
- **Borne** : si l'appel n'est toujours pas passé `escalation_delay_minutes + CALL_WAIT_MAX_MINUTES` (= **3 min**) après le début du palier, l'escalade part quand même (une passerelle ou un modem en panne ne doit pas bloquer toute la chaîne), avec l'événement d'audit `escalation_without_call` et un email à `alert_email`. Choix du propriétaire du 2026-10-20 (+3 min).
- Hors champ : `/api/alarms/escalate-now` (bouton admin), `/api/test/trigger-escalation` et l'escalade par touche 2 au téléphone ne passent pas par la boucle.
- **Pourquoi** : prod 2026-10-20, alarme 852 : délais d'escalade et de SMS/appel à 2 min ; escalade à 10:13:55, SMS et appel de timo créés à 10:13:57, appel passé à 10:15:28 — après l'escalade. L'alarme 818 : appel retardé ~3 min par un modem instable, parti après l'escalade. Le propriétaire exige que SMS et appel précèdent toujours l'escalade.
- **Couverture** : Unit `test_escalation.py::TestWaitForCallInv144` (borne exacte, attente, défaut inchangé) ; Integration `test_escalation_waits_for_call_inv144.py` (vraie boucle : retenue puis escalade dès SMS et appel passés ; appel `error` rejoué et retenu ; SMS en échec retenu jusqu'à son envoi ; escalade forcée à délai + 3 min avec audit + email).

### INV-154 [C] ✅ Alarme acquittée ou résolue : plus aucun SMS ni appel pour elle (2026-10-22)
Dès qu'une alarme est acquittée (quel que soit le moyen : web, app, touche 1, réponse SMS) ou résolue, ses SMS et appels **pas encore partis** ne sont plus remis à la gateway (`/internal/sms/pending`, `/internal/calls/pending`), et la boucle d'escalade les retire de la file à son tick suivant : SMS `retries = 3` + `error = "annulé : alarme acquittée ou résolue"`, appel `retries = 3` + `result = "cancelled"`. Le schéma d'escalade (INV-136) les affiche « annulé ».
- **Rattachement** d'un SMS à son alarme (`sms_queue` n'a pas d'`alarm_id`) : la dernière notification, au plus tard à la création du SMS, d'un utilisateur ayant ce numéro. Un SMS sans telle notification (envoi de test, SMS hors alarme) n'est pas concerné.
- Si l'acquittement expire (INV-016), l'alarme repart avec de nouveaux SMS et appels (INV-017).
- **Hors champ** : un SMS ou un appel déjà remis à la gateway et en cours d'envoi au moment de l'acquittement va au bout.
- **Pourquoi** : prod 2026-10-22, alarme 923 : acquittée à 12:14:51 et résolue à 12:15:05 (UTC). Gênée par un modem instable (`AT_ERROR`), la gateway rejouait encore SMS et appel : SMS à 12:16:15, appel décroché à 12:16:28. Le propriétaire a reçu un SMS et un appel après l'acquittement : « ça ne doit pas être possible ».
- **Couverture** : Integration `test_no_contact_after_ack_inv154.py` ; expiration de l'ack : `test_ack_expiry_restarts_with_new_sms_and_call` (nouveaux SMS et appel remis à la gateway, ids différents) et `test_escalation_after_ack_expiry_waits_for_the_new_call` (INV-144 : l'appel annulé de l'ancien cycle ne compte pas comme passé ; RED avant le correctif : escalade avant le nouvel appel, issue #260).

### INV-155 [C] ✅ Multi-site : chaque passerelle et chaque alarme portent un site (2026-10-22)
Sites connus : `SystemConfig sites`, par défaut `izeron,beaufort`. `SystemConfig default_site`, par défaut `izeron`.
- **Passerelle** : variable `GATEWAY_SITE` (un site), celui de la **machine** (`/etc/alarm-gateway.env`, posé au provisionnement, INV-182) : le modem et l'Arduino prennent le site de la machine où ils sont branchés. Elle est envoyée dans `POST /internal/alarms/report-state`, `POST /internal/alarms/modem-status` et en paramètre `site` de `/internal/sms/pending` et `/internal/calls/pending`. Absente (passerelle d'avant cet invariant), elle vaut `default_site`. Une liste (`izeron,beaufort`) n'est admise que sur le cluster de test, pour qu'un seul modem joue les deux sites.
- **Alarme** (`alarms.site`, migration) :
  - contact sec → le site de la passerelle qui l'a déclenché ;
  - alarme manuelle (web, `POST /api/alarms/send`) → `site` obligatoire, parmi les sites connus (`422` sinon) ;
  - alarme d'astreinte (perte de heartbeat) → `site` vide ; ses SMS et appels partent par `default_site`.
- **Affichage** : le titre de l'alarme contact sec nomme le site (« Déclenchement contact sec — Izeron »). Le SMS commence par le site (`ALARME Izeron: …`). Le site apparaît dans le push, l'app (écran d'alarme, dashboard, notification) et le web (tableau de bord, historique).
- **Inchangé** : une seule chaîne d'escalade pour tous les sites (même équipe de garde). Délais, acquittement, expiration, INV-144/145/148 s'appliquent alarme par alarme.
- **Pourquoi** : Izeron (aujourd'hui onsite-1/2, demain alarm-1/2) et Beaufort (alarm-3/4) formeront **un seul cluster de 5 nœuds**, avec la même APK et la même équipe de garde. Décision du propriétaire du 2026-10-22.
- **Couverture** : Integration `test_multisite_inv155_156.py` : `test_izeron_and_beaufort_alarms_coexist`, `test_send_without_or_unknown_site_is_422` (sans site, site inconnu), `test_dry_contact_beaufort_while_izeron_active` (titre « — Beaufort »), `test_dry_contact_without_site_uses_default_site`, `test_sms_and_calls_carry_site_and_pending_filters_by_site` (`ALARME Izeron:`), `test_oncall_alarm_has_no_site_and_contacts_go_to_default_site`, `test_known_sites_exposed_for_web_form` (`GET /api/config/sites`, formulaire web).
- **Push** : tout push d'alarme (création, escalade, rappel, réveil, contact sec, `alarm_resolved`) porte `data["site"]` (`""` pour une alarme sans site). Integration `test_multisite_inv157_158_160.py::test_alarm_pushes_carry_site`.
- **Reste** : le site dans l'app Android (PR Android à venir, elle lit `data["site"]`). Le web affiche « Izeron · titre » (tableau de bord, alarmes, schéma d'escalade), non couvert par Playwright.

### INV-156 [C] ✅ Multi-site : les SMS et appels d'une alarme partent par le modem de son site
`sms_queue.site` et `call_queue.site` reçoivent le site de l'alarme (ou `default_site` pour une alarme sans site). `/internal/sms/pending?site=X` et `/internal/calls/pending?site=X` ne renvoient **que** les lignes du site X. Un SMS ou un appel n'est donc jamais remis à deux passerelles (plus de double envoi), à condition qu'**un seul modem** soit branché par site (INV-182) ; une passerelle sans modem ne lit pas la file (INV-183). **Révisé le 2026-11-12** : plusieurs modems par site ; chaque élément est réservé par une seule passerelle avant d'agir (INV-193).
- **Pas de secours croisé** (choix du propriétaire) : si le modem d'un site est en panne, ses SMS et appels attendent ce modem (INV-145), et l'escalade continue après délai + 3 min (INV-144).
- SMS avant l'appel (INV-134), SMS rattaché à son alarme (INV-154) : inchangés, à l'intérieur d'un même site.
- **Pourquoi** : sans site, deux passerelles liraient la même file et enverraient chaque SMS et chaque appel deux fois.
- Une ligne sans site (créée avant la migration) part par `default_site`.
- **Couverture** : Integration `test_multisite_inv155_156.py` : `test_sms_and_calls_carry_site_and_pending_filters_by_site` (site des lignes, filtre `?site=`, sans paramètre → `default_site`, liste `izeron,beaufort`), `test_oncall_alarm_has_no_site_and_contacts_go_to_default_site`.

### INV-157 [H] ✅ Multi-site : contact sec agrégé et désaccord détecté site par site (révise INV-122/123)
L'agrégation OU (INV-122), la détection de désaccord (INV-123) et la résolution automatique de l'alarme contact sec ne comparent que les passerelles **vivantes d'un même site**. Une alarme contact sec d'Izeron ne se résout qu'avec les passerelles d'Izeron.
- Le site de la passerelle est rangé dans `gateway_states.site` (migration) à chaque `report-state` (champ `site`, absent → `default_site`). Une ligne sans site (écrite avant la migration) compte pour `default_site`.
- La réponse `{alarm_active, dissensus}` et l'email de désaccord ne portent que sur le site de la passerelle qui rapporte ; l'email nomme le site.
- **Pourquoi** : sinon, Beaufort ouvert et Izeron fermé passerait pour une panne de câblage (email « dissensus ») au lieu d'une alarme Beaufort, et la fermeture à Izeron pourrait résoudre l'alarme de Beaufort.
- **Couverture** : Integration `test_multisite_inv157_158_160.py` : `test_beaufort_open_izeron_closed_is_not_a_dissensus` (pas d'alarme Izeron, pas de désaccord ; désaccord entre 2 passerelles de Beaufort toujours détecté), `test_closing_a_site_resolves_only_its_own_alarm` (dans les deux sens ; passerelle sans site = Izeron). RED avant le correctif : Izeron fermé créait une alarme Izeron et levait un désaccord ; Izeron fermé ne résolvait pas son alarme tant que Beaufort était ouvert.

### INV-158 [H] ✅ Multi-site : état du modem par passerelle
`POST /internal/alarms/modem-status` est rangé par `gateway_id`, avec son site (`{registered, tech, rsrp, csq, gateway_id, site}`, `site` absent → `default_site`). `GET /api/system/modem-status` renvoie la **liste** `gateways: [{gateway_id, site, state, bars, tech, rsrp, updated_at}]` (règles d'INV-129 pour chaque élément, triée par site puis `gateway_id`). Le web et l'app affichent un indicateur de signal par passerelle, nommé par son site (un par site quand chaque site a un modem).
- **Forme de la réponse** (compatible avec l'app d'avant cet invariant, qui lit un objet) : `{gateway_id, site, state, bars, tech, rsrp, updated_at, gateways: [...]}`. Les champs de premier niveau reprennent l'entrée la **plus mal en point** (le moins de barres ; à égalité, la première de la liste), pour qu'une ancienne app signale la panne d'un modem au lieu de la masquer. Aucune passerelle : `gateways: []` et premier niveau `{gateway_id: null, site: null, state: "unknown", bars: 0, tech: null, rsrp: null, updated_at: null}`.
- **Passerelle d'avant cet invariant** (sans `gateway_id`) : entrée `gateway_id = "legacy"` (ancienne clé `modem_status`, site du rapport ou `default_site`). Muette depuis plus de 180 s, elle n'est plus listée : c'est l'ancienne passerelle, mise à jour depuis, qui rapporte maintenant sous son `gateway_id`.
- **Web** : `#modemSignal` contient un `.modem-signal` par entrée (`data-gateway`, `data-site`, `data-state`, `data-bars`, nom du site affiché, libellé « Izeron (alarm-1) : … dBm ») ; aucune entrée → un seul indicateur « inconnu ».
- **Pourquoi** : une seule clé `modem_status`, écrasée par chaque passerelle, masquerait la panne du modem d'un site.
- **Couverture** : Integration `test_multisite_inv157_158_160.py` : `test_modem_status_listed_per_gateway_with_its_site` (liste, site, premier niveau = la plus mal en point), `test_each_gateway_goes_unknown_on_its_own` (180 s par passerelle), `test_gateway_without_gateway_id_is_legacy` (réponse vide, `legacy`, `legacy` muette retirée) ; `test_modem_status_inv129.py` (inchangé, passerelle sans `gateway_id`). Playwright `test_frontend.py::TestModemSignalIndicator` (un indicateur par passerelle, nom du site). App Android : PR séparée.

### INV-159 [M] ✅ Multi-site : l'appel vocal annonce le site (2026-10-29)
Le message de Jessica commence par le site : fichiers `alarme_izeron.wav` et `alarme_beaufort.wav` (même voix et réglages que `alarme.wav`, version A). Repli TTS du module : le texte nomme aussi le site. `alarme.wav` reste utilisé pour une alarme sans site.
- **Textes** (courts, demande du propriétaire du 2026-10-30) : « Alarme Izeron. Appuyez sur 1 pour acquitter, 2 pour escalader. » / « Alarme Beaufort. … » ; `alarme.wav` : « Alarme Murgat. … ». Le détail de l'alarme est dans le SMS, qui précède l'appel (INV-134). Générés par `scripts/generate-gateway-voice.py`.
- **Site annoncé** : `/internal/calls/pending` donne le `site` **de l'alarme** de chaque appel (`null` pour une alarme d'astreinte, même si son appel part par le modem de `default_site`, INV-156). La gateway joue `alarme_<site>.wav`, sinon `alarme.wav` (alarme sans site, site sans message, champ absent d'un backend d'avant, fichier absent d'une voix d'avant pendant le déploiement : jamais d'erreur). Repli TTS : « Site Izeron. » devant le texte du backend.
- **Pourquoi** : la même personne de garde couvre les deux sites ; elle doit savoir où intervenir dès le décroché. Demande du propriétaire du 2026-10-29 : « Je veux que Jessica lors de l'appel donne le nom du site Beaufort ou Izeron ».
- **Couverture** : Unit `test_gateway_voice_call.py::TestSiteVoice` (fichier par site, repli `alarme.wav`, texte TTS, WAV livrés au format de `alarme.wav`) ; Integration `test_call_site_inv159.py` (Izeron, Beaufort, astreinte → `null`). RED avant le correctif : fonctions absentes, champ `site` absent de la file. Validation matérielle (appel réel) : à faire après déploiement.

### INV-160 [C] ✅ Multi-site : la réponse « 1 » par SMS acquitte l'alarme du dernier SMS reçu (2026-10-23)
Quand la gateway transmet une réponse d'acquittement par SMS (`POST /internal/alarms/active/ack-by-phone`, INV-124), l'alarme acquittée est celle du **dernier SMS d'alarme envoyé à ce numéro** (`sms_queue.sent_at` le plus récent) : l'alarme non résolue de son site (`sms_queue.site`, INV-156) pour laquelle l'utilisateur a été notifié. L'alarme de l'autre site n'est pas touchée et continue (escalade, sonnerie).
- Aucun SMS envoyé à ce numéro pour une alarme en cours → comportement d'avant : la première alarme en cours pour laquelle l'utilisateur a été notifié.
- La touche 1 pendant un appel acquitte déjà l'alarme de cet appel (`call_queue.alarm_id`) : inchangé.
- **Pourquoi** : avec deux alarmes en cours (Izeron et Beaufort), « la première alarme trouvée » pouvait acquitter le mauvais site. Le SMS nomme son site (INV-155) : la personne de garde répond à celui qu'elle vient de lire. Choix du propriétaire du 2026-10-23.
- **Couverture** : Integration `test_multisite_inv157_158_160.py` : `test_sms_reply_acks_alarm_of_last_sms_received[izeron|beaufort]` (l'autre site reste en cours ; RED avant le correctif pour Beaufort : Izeron acquittée), `test_sms_reply_falls_back_when_last_sms_alarm_is_over` (alarme du dernier SMS résolue → repli). Repli sans SMS envoyé : E2E `test_sms_voice.py::TestAckByPhone`.

### INV-147 [C] ✅ Résolution : chaque utilisateur notifié reçoit un push `alarm_resolved`
Quand une alarme passe `resolved` — depuis le web ou l'app (`POST /api/alarms/{id}/resolve`) ou par la résolution automatique de la gateway (`/internal/alarms/report-state` à `closed`, INV-120) — le backend envoie, après le commit, un FCM haute priorité data-only à **chaque** utilisateur de `alarm_notifications` : `data = {type: "alarm_resolved", alarm_id, title: "Alarme resolue", body: <titre de l'alarme>}`. Best-effort : un échec FCM n'empêche pas la résolution. Le traitement côté téléphone est INV-ANDROID-210.
- **Pourquoi** : prod 2026-10-21, alarme 886 résolue depuis le web. Le Pixel 9a de admin, verrouillé, a continué de sonner jusqu'à ce que le propriétaire le déverrouille ; le téléphone de timo a coupé le son mais gardé l'écran d'alarme. Aucun push n'était envoyé à la résolution : un téléphone en veille ne l'apprenait qu'à son prochain poll réussi, que Doze peut retarder indéfiniment.
- **Couverture** : Integration `test_resolve_push_inv147.py` (résolution web avec 2 notifiés après escalade ; résolution automatique gateway). RED avant le correctif : aucun push.

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
- ✅ `alarms.py:167` — ORDER BY pour `/mine` — issue #208
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

### INV-034 [C] ✅ `/mine` : alarmes à acquitter en tête de liste
`GET /api/alarms/mine` renvoie d'abord les alarmes non acquittées (`active`/`escalated`), puis les `acknowledged` ; dans chaque groupe, la plus récente en premier selon `original_created_at` (INV-018b, pas le timer `created_at`).
- **Pourquoi** : l'app Android affiche et fait acquitter le **premier** élément. Une alarme acquittée (suspendue 30 min) ne bloque pas une nouvelle (INV-001) ; sans ce tri, la nouvelle restait derrière l'ancienne et n'était jamais acquittable. Incident prod 2026-10-13 : #526 escaladée sans possibilité d'ACK, #525 acquittée en tête. Règle validée par le propriétaire le 2026-10-13 (tranche la question ouverte `android/INVARIANTS.md` §6 « 2e alarme pendant la 1ère »).
- **Couverture** : E2E `TestAckedAlarmVisibility::test_unacked_alarm_listed_before_acked_one` (RED avant le fix).
  - `test_mine_unacked_first_then_acked_most_recent_first` (tue les mutants « sans critère status » et « `.asc()` ») et `test_mine_orders_by_original_created_at` (RED avant l'alignement sur `original_created_at`) — issue #208.

---

## 4. Heartbeat et watchdog

### INV-040 [C] ⚠️ Heartbeat met online + timestamp
POST /devices/heartbeat → `user.is_online = True`, `user.last_heartbeat = now`.

### INV-041 [H] ⚠️ Watchdog détecte les offlines
Tous les 30s, si `user.last_heartbeat < now - 60s` ET `is_online=True` → `is_online=False`.
- **Pourquoi** : détection de déconnexion non-annoncée.

<!-- INV-042 déplacé en section 12 (meta / outils de test) -->

### INV-043 [M] ⚠️ Heartbeat sur replica → proxy au leader (200) ; 503 seulement si aucun leader joignable (révisé 2026-06-17)
Un heartbeat envoyé à un noeud replica est **forwardé au leader** via WireGuard (`_proxy_heartbeat_to_primary`) qui exécute l'écriture et renvoie 200. Comportement **uniforme sur tous les nœuds** (`HEARTBEAT_PROXY_ON_REPLICA`, défaut **true**). Le 503 n'est renvoyé que si **aucun leader n'est joignable** (panne cluster / pas de quorum) ou si le flag kill-switch est mis à `false`.
- **Pourquoi** : à terme **n'importe quel nœud peut être joignable depuis l'extérieur** (pas seulement le cloud). Un téléphone externe/mobile qui tombe sur un replica ne peut PAS rotater vers les autres nœuds (LAN privé, ou cloud tombé) → s'il recevait 503, "connexion perdue" perpétuelle alors que le cluster est sain. En relayant côté backend, **tout** nœud edge sait servir un client externe, même replica. Règle uniforme = pas de point de défaillance « seul le cloud sait relayer ». Le leader reste où Patroni l'a élu (souvent onsite, bon pour la gateway SMS) ; les replicas ne sont que des relais d'entrée.
- **Anti-boucle** : le proxy ajoute le header `X-Heartbeat-Proxied: 1`. Un nœud qui reçoit un heartbeat déjà proxifié ne le re-proxifie PAS (il l'exécute s'il est leader, sinon 503). Le nœud d'entrée itère ses peers jusqu'au leader ; pas de récursion replica→replica.
- **Évolution** : avant 2026-06-16, replica → 503 sec (l'app rotatait sur le LAN, cf INV-ANDROID-304). Étape intermédiaire 2026-06-16 : proxy activé uniquement sur le nœud cloud. Depuis 2026-06-17 : proxy par défaut partout (décision propriétaire — résilience à la chute du cloud + onsite bientôt joignables de l'extérieur).
- **Kill-switch** : `HEARTBEAT_PROXY_ON_REPLICA=false` restaure le 503 sec (chemin testé). En dev/CI single-node le nœud est leader → proxy jamais déclenché.
- **Couverture** : `tests/integration/test_heartbeat_proxy_inv043.py` (tier 2, 4 tests) : leader→200, replica+kill-switch→503, replica défaut→proxy 200, replica+déjà-proxifié→503 (anti-boucle). + `test_failback.py` (tier 4) valide la **continuité** des heartbeats à travers un failover (l'app garde le 200 via le nouveau leader, relayé ou direct), plus la "rotation vers le leader exact".
- **INV-173 (2026-10-30)** : le relais essaie d'abord le peer dont l'hôte est le leader Patroni local, `httpx.Timeout(3.0, connect=1.5)`.

### INV-044 [M] ⚠️ Écriture `/api/*` reçue par un replica → proxy au leader (nouveau 2026-06-17)
Toute requête **mutante** (`POST`/`PUT`/`PATCH`/`DELETE`) sur un chemin `/api/*` reçue par un nœud **replica** est **forwardée au leader** (via WireGuard, mêmes peers `PEER_TEST_URLS` que INV-043) qui exécute l'écriture ; la réponse du leader est **relayée telle quelle** (200/401/429/…). C'est la **généralisation de INV-043** (heartbeat) à **toutes les écritures applicatives**. Kill-switch `WRITE_PROXY_ON_REPLICA`, défaut **true** sur tous les nœuds.
- **Pourquoi** : l'**interface web** est servie par un nœud **fixe** (souvent le cloud OVH, seul joignable publiquement) et **ne peut pas faire tourner les URLs** comme l'app mobile (INV-ANDROID-304). Si ce nœud est replica, `POST /api/auth/login` — qui **écrit** (refresh token persistant INV-079 + `log_event`) — échoue en `ReadOnlySqlTransaction` (500) → **impossible de se connecter**. En relayant côté backend, l'UI web (login, config escalade, CRUD users) fonctionne **quel que soit le primary**, ce qui permet de garder le leader Patroni sur un **onsite** (bon pour l'écriture via la gateway SMS) sans casser l'admin web depuis le cloud.
- **Périmètre** : seuls les chemins `/api/*` sont relayés. **Exclus** (endpoints qui gèrent eux-mêmes leur comportement replica) : `/api/devices/heartbeat` (relai dédié INV-043, propre header anti-boucle `X-Heartbeat-Proxied`), `/api/deployments/*` (plumbing CD auth gateway-key : `POST /events` a son propre garde replica→503 et les scripts CD découvrent le leader via `discover_leader()`/`GET /health`), et `/api/test/*` (fan-out propre + désactivé en prod, INV-076). Les endpoints **node-to-node** sont sous `/internal/*` (gateway SMS/voix `/internal/sms` `/internal`, `alarms_internal` `/internal/alarms`) : ils ne commencent pas par `/api/` → **jamais relayés**, leur routage est géré en amont.
- **Anti-boucle** : le proxy ajoute `X-Write-Proxied: 1`. Un replica qui reçoit une écriture **déjà proxifiée** ne la ré-exécute PAS (il n'est pas leader) ni ne la re-proxifie : il renvoie `503` + header `X-Write-Proxy-Declined: 1`, signal qui fait passer le nœud d'entrée au **peer suivant** jusqu'au leader. Pas de récursion replica→replica.
- **Kill-switch** : `WRITE_PROXY_ON_REPLICA=false` restaure l'ancien comportement (l'écriture atteint l'endpoint local et échoue en read-only). En dev/CI single-node le nœud est leader → proxy jamais déclenché (aucun impact sur la suite de tests existante).
- **Couverture** : `tests/integration/test_write_proxy_inv044.py` (tier 2) : leader→exécute local, replica défaut→proxy, replica déjà-proxifié→503+`X-Write-Proxy-Declined` (anti-boucle), kill-switch→pas de proxy, `/api/devices/heartbeat` exclu (INV-043 garde la main), `/api/deployments` exclu, GET non relayé.
- **INV-173 (2026-10-30)** : le relais essaie d'abord le peer dont l'hôte est le leader Patroni local, `httpx.Timeout(5.0, connect=1.5)`.

---

## 5. Astreinte (oncall)

> **Changement de stratégie 2026-05-26** (révision 2026-06-03 — architecture client/serveur INV-308) : la création automatique d'une alarme "oncall_offline" (INV-050) générait trop de faux positifs — un opérateur qui perd sa data (4G/Wi-Fi) mais garde son signal cellulaire (SMS + voix) est marqué offline alors qu'il reste joignable. Décision : **supprimer la création d'alarme** sur user pos 1 offline, et la **remplacer par un tracking statistique** des transitions online ↔ offline (cf nouveau INV-056). L'email "personne en ligne" (INV-053) reste, car il couvre un cas business différent (sortie de boucle quand le système ne sait plus à qui parler).
>
> **Architecture client/serveur de la détection « hors connexion »** (révisée 2026-06-03) : la responsabilité de prévenir l'opérateur quand son téléphone perd contact avec le back se joue maintenant en deux étages couplés.
> - **Étage backend (INV-067)** : dès que le heartbeat HTTP du pos 1 est KO depuis ≥ 30 s, le back envoie un SMS `[ALARME-MURGAT-PING] <iso_ts>` au pos 1, puis le rejoue toutes les 2 min tant que le heartbeat reste KO.
> - **Étage client (INV-ANDROID-308)** : quand l'app constate `heartbeatLostSince` ≥ 5 min sans avoir reçu un seul de ces SMS pendant la fenêtre, elle déclenche une sonnerie locale + snooze 5 min × 3 max. Si un SMS PING arrive dans la fenêtre, seul un bandeau info s'affiche (pas de son) — le canal SMS prouve que l'opérateur est joignable hors-bande.
>
> L'ancienne référence `INV-ANDROID-302/305` (sonnerie sur perte de réseau total via callbacks `ConnectivityManager` + `TelephonyManager`) est marquée ❌ SUPERSEDED depuis cette révision : impossible à fiabiliser sur Crosscall Core-M6 / Android 15 (callbacks non émis, `ServiceState` rédigé). L'absence de SMS PING devient la **preuve indirecte** qu'au moins un des trois maillons (back, gateway SIM7600, opérateur cellulaire opérateur de garde) est cassé — sans avoir à interroger des APIs telephony non fiables.

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

### INV-126 [C] ✅ Gateway : modem muet → sortie pour relance systemd et re-détection du port
Si le keepalive `AT` (toutes les 60 s) échoue `MAX_AT_FAILURES` = 2 fois de suite, la gateway sort (code 1) ; systemd (`Restart=always`) la relance et `detect_modem_port` retrouve le port AT. Un échec isolé ne suffit pas.
- **Modem absent** (aucun port SIMCom sur l'USB) : la gateway relancée l'attend sans sortir ; présent mais muet à 2 recherches de suite : elle sort, comme ici (INV-183).
- **Pourquoi** : incident onsite-2 du 2026-09-12 07:11 au 2026-10-13. Le SIM7600 s'est ré-énuméré sur l'USB (`ttyUSB4` disparu, AT passé sur `ttyUSB3`). La gateway a gardé l'ancien port pendant un mois (`Input/output error` chaque seconde, 45 212 logs « tentative de reconnexion » sans reconnexion). Le watchdog modem ne voyait rien : le modem était bien présent sur l'USB. Résultat : **aucun SMS ni appel réellement émis pendant un mois**, sans alerte. Règle validée par le propriétaire le 2026-10-13.
- **Couverture** : Unit `test_gateway_modem_health.py::TestKeepaliveExit`.

### INV-127 [H] ✅ Gateway : appel non émis → résultat `error`, jamais `no_answer`
Un appel n'est compté « passé » que si le modem répond `OK` à `ATD<numéro>;`. Toute autre réponse, **y compris vide** (port série mort : `send_at_command` avale l'erreur et renvoie `""`), donne le résultat `error`.
- **Pourquoi** : pendant l'incident INV-126, les appels étaient enregistrés `no_answer` alors qu'aucun téléphone n'avait sonné. L'opérateur croyait à un défaut de décroché au lieu d'une panne totale du canal. Règle validée par le propriétaire le 2026-10-13.
- **Couverture** : Unit `test_gateway_modem_health.py::TestCallWasDialed`.

### INV-128 [C] ✅ Gateway : modem sans réseau 5 min → radio coupée puis rallumée, une fois par épisode
À chaque keepalive (60 s), la gateway lit `AT+CREG?`. Si le modem n'est inscrit nulle part (stat ≠ 1 et ≠ 5) pendant `UNREGISTERED_CHECKS_BEFORE_RECOVERY` = 5 contrôles consécutifs, elle envoie `AT+CFUN=0` puis `AT+CFUN=1`, **une seule fois par épisode**. Le compteur se remet à zéro dès que le modem est de nouveau inscrit.
- **Pourquoi** : le 2026-10-13, onsite-2 était resté désinscrit (`+COPS: 2`, `+CME ERROR: SIM failure` sur `COPS=0`) depuis la ré-énumération USB du 2026-09-12. Pendant tout ce temps, aucun SMS ni appel n'était émis. Un cycle `CFUN=0/1` l'a réinscrit en 26 s. Il ne s'agit pas d'un reset modem : le lien USB n'est pas touché. Règle validée par le propriétaire le 2026-10-13.
- **Couverture** : Unit `test_gateway_modem_health.py::TestParseNetwork`, `::TestReregisterDue`.

### INV-145 [C] ✅ Gateway : aucun essai de SMS/appel tant que le modem est muet ou sans réseau
La gateway ne tente un SMS ou un appel que si son dernier keepalive (60 s, contrôle immédiat au démarrage) a obtenu `OK` à `AT` **et** un modem inscrit (`AT+CREG?` stat 1 ou 5). Sinon les SMS et appels restent en file **sans consommer d'essai** (INV-064) et partent dès que le modem est de nouveau prêt.
- **Pourquoi** : alarme 886 du 2026-10-21 (admin). Modem désinscrit puis muet : le SMS a échoué 3 fois en 1 min 30 (`AT_ERROR`, `CMGF_FAILED`) et a été **abandonné** ; il ne serait jamais parti, même après le retour du modem. Le propriétaire exige que SMS et appel partent absolument.
- **Couverture** : Unit `test_gateway_modem_health.py::TestSendsAllowed`.

### INV-146 [C] ✅ Watchdog : modem présent mais muet → reset USB automatique
`alarm-modem-watchdog` suit les relances de la gateway (`NRestarts` systemd ; la gateway sort quand le modem est muet, INV-126). **3 relances en 10 min** = modem muet : reset USB du seul SIM7600 (`authorized` 0 → 1 sur le périphérique `1e0e`), puis relance de `alarm-modem-rat`, de la gateway et de `alarm-4g-standby`. **Un seul email par épisode** ; l'épisode se clôt après 10 min sans relance. Le cas « modem absent de l'USB » (PCI rescan) : INV-184 (seulement un modem déjà vu, une fois par épisode).
- **Pourquoi** : alarme 886 du 2026-10-21 : SIM7600 visible sur l'USB mais port inutilisable (`BrokenPipeError`), gateway relancée toutes les ~90 s. Le watchdog ne réagissait qu'à un modem absent : aucune récupération. Un reset USB manuel (`authorized` 0 → 1) l'a rétabli en 30 s.
- **Couverture** : Unit `test_modem_watchdog.py::TestMuteDetected`.

### INV-129 [H] ✅ Signal 4G du modem visible dans l'app et le web, « inconnu » si la gateway se tait
La gateway envoie à chaque keepalive `POST /internal/alarms/modem-status` (`X-Gateway-Key`, INV-065) `{registered, tech, rsrp, csq}`. `GET /api/system/modem-status` (tout utilisateur connecté) renvoie `{state, bars, tech, rsrp, updated_at}` (multi-site : un état par passerelle, cf. INV-158) :
- `state = unknown`, `bars = 0` si aucun rapport ou rapport de plus de **180 s** (3 keepalives manqués) ;
- `state = no_network`, `bars = 0` si le modem n'est pas inscrit ;
- sinon `state = ok`, barres tirées du RSRP en LTE (≥ −90 → 4, ≥ −100 → 3, ≥ −110 → 2, sinon 1), ou à défaut du CSQ (≥ 20 → 4, ≥ 15 → 3, ≥ 10 → 2, sinon 1).

Le web affiche `#modemSignal` (4 barres animées, `data-state` et `data-bars`, avec un libellé contenant les dBm). L'état sans réseau est rouge et pulsé.
- **Pourquoi** : la perte de réseau du modem (INV-128) est restée invisible pendant un mois. Le propriétaire a demandé le 2026-10-13 une icône de signal en temps réel dans l'app et le web plutôt qu'un email. Mesure de référence onsite-2 : RSRP −116 dBm (1 barre, bordure de cellule).
- **Couverture** : Unit `test_modem_signal.py` (mutation 100 %) ; Integration `test_modem_status_inv129.py` ; E2E Playwright `test_frontend.py::TestModemSignalIndicator`. App Android : PR séparée.

### INV-130 [H] ✅ Web : toute date affichée est convertie depuis l'UTC vers l'heure locale du navigateur
Les dates de l'API sont de l'UTC naïf (`isoformat()` sans `Z`). Le web les lit avec `parseUtc()`, qui ajoute le `Z` si aucun fuseau n'est présent. C'est le cas du Journal (onglet Log), de l'onglet Disponibilité (tableau et graphe) et de `timeAgo`.
- **Pourquoi** : sans `Z`, `new Date()` lit la chaîne comme de l'heure locale. Le Journal et la Disponibilité affichaient alors l'heure UTC (2 h d'écart l'été à Paris). Constaté le 2026-10-15 en analysant le contact sec du 14/10.
- **Couverture** : Playwright `test_frontend.py::TestTimesShownInLocalTime` (navigateur réglé sur Europe/Paris). RED sur le code d'origine : 06:58 affiché au lieu de 08:58.

### INV-131 [H] ✅ Gateway : appel « décroché » seulement quand le modem le dit actif
L'appel n'est compté décroché que sur l'URC `VOICE CALL: BEGIN` **ou** une ligne `+CLCC` dont le **3ᵉ champ** (`stat`) vaut `0`. Les états numérotation (`2`) et sonnerie (`3`) ne comptent pas. `NO CARRIER`, `BUSY` ou `VOICE CALL: END` avant le décroché → `no_answer` si le téléphone a sonné, sinon `error` (INV-148).
- **Pourquoi** : test réel du 2026-10-14 sur alarm-1 (SIM7600, Orange). La gateway cherchait `",0,0,"` n'importe où dans la réponse : `+CLCC: 3,0,2,0,0,"+33…",145` (numérotation) contient `,0,0,`. Elle croyait l'appel décroché avant même la sonnerie, jouait le message dans le vide, puis raccrochait en `no_answer` alors que le propriétaire avait bien décroché.
- **Couverture** : Unit `test_gateway_voice_call.py::TestCallAnswered`, `::TestCallEnded`.

### INV-132 [H] ✅ Gateway : message d'alarme en voix française, audible, juste après le décroché
Une fois l'appel décroché, la gateway joue le message `voice/alarme.wav` (PCM 16 bits mono, 8 ou 16 kHz) en l'injectant dans l'appel par le port audio USB du SIM7600 (interface USB `:1.4`, **ouvert avant** `AT+CPCMFRM`/`AT+CPCMREG=1` et **fermé après** `AT+CPCMREG=0`), au rythme réel (trames de 20 ms), précédé de **0,8 s** de silence. Si le fichier ou le port audio manque, ou si l'injection s'arrête alors que l'appel continue (INV-169), elle se rabat sur la synthèse du module **avec `AT+CDTAM=1`** (sinon le son reste local au module et l'appelé n'entend rien).
- **Pourquoi** : tests réels du 2026-10-14. (1) `AT+CDTAM` vaut `0` par défaut : le message `AT+CTTS` n'a jamais été entendu au bout du fil. (2) La synthèse du module ne parle qu'anglais. (3) `AT+CCMXPLAYWAV` répond `ERROR` sur ce firmware (LE20B04SIM7600M22), l'injection PCM USB fonctionne. Voix retenue par le propriétaire : Piper `fr_FR-upmc-medium`, locutrice `jessica`, débit 1,0, traitement téléphonique, 16 kHz (appel en VoLTE). Sans silence, le message commençait trop tôt ; avec ~3-5 s d'attente, trop tard. (4) Le 2026-10-15, avec le port audio ouvert **après** `AT+CPCMREG=1` et fermé **avant** `AT+CPCMREG=0`, l'appel suivant un appel réussi restait muet (envoi bloqué au bout de ~14 s) jusqu'au redémarrage du modem ; dans l'ordre de l'exemple SIMCom, 3 appels d'affilée (dont un raccroché pendant le message) ont tous été entendus.
- **Couverture** : Unit `test_gateway_voice_call.py::TestAudioPort`, `::TestPcmFrames`, `::TestLeadSilence`. Validation matérielle : appel réel sur alarm-1.

### INV-133 [M] ✅ Gateway : touche 1/2 reconnue même pendant le message, et confirmée à voix haute
La touche est lue sur les URC `+RXDTMF: <k>` (ou `+DTMF: <k>`), y compris pendant la lecture du message, qui s'interrompt alors. Sans touche, la gateway attend **30 s** après la fin du message. Touche `1` → confirmation `voice/acquittee.wav` puis résultat `ack_dtmf` ; touche `2` → `voice/escaladee.wav` puis `escalate` ; aucune touche → `no_answer`. La gateway **raccroche aussitôt la confirmation jouée** (`AT+CHUP`, commande de raccrochage des appels vocaux SIMCom, `ATH` en secours) : l'appelé n'a pas à raccrocher lui-même.
- **Pourquoi** : test réel du 2026-10-14 : `+RXDTMF: 1` est bien émis par ce SIM7600 (contrairement à `ARCHITECTURE_SMS_VOIX.md`, qui le dit non supporté). Le propriétaire veut entendre que sa touche a été prise en compte.
- **Couverture** : Unit `test_gateway_voice_call.py::TestDtmfKey`, `::TestCallResult`, `::TestConfirmation`, `::TestHangUp`.

### INV-148 [C] ✅ Gateway : `no_answer` seulement si le téléphone a sonné, sinon `error` (rejoué)
Un appel non décroché n'est enregistré `no_answer` que si le modem a signalé la **sonnerie** chez le destinataire : une ligne `+CLCC` avec `stat` = 3 (alerte) ou 0 (actif) pendant l'attente. Sinon, l'appel s'est perdu sur le réseau avant de sonner : résultat `error`, donc rejoué (INV-144, au plus 3 essais INV-064). Précise INV-131 (`NO CARRIER` avant décroché).
- **Pourquoi** : alarme 888 du 2026-10-22, onsite-2 en 3G à CSQ 3. `ATD` accepté, mais le téléphone de timo n'a jamais sonné ; l'appel a été enregistré `no_answer`, compté comme passé, donc jamais rejoué. Le propriétaire exige que l'appel parte absolument.
- **Couverture** : Unit `test_gateway_voice_call.py::TestCallRang`.

### INV-169 [C] ✅ Gateway : canal audio bloqué → modem redémarré, et l'appel reste audible (2026-10-29)
Le canal audio PCM est **bloqué** quand, hors appel, `AT+CPCMREG?` vaut `1` et que `AT+CPCMREG=0` ne le ramène pas à `0` : réponse `ERROR`, ou toujours `1` à la relecture. Une relecture à `0`, ou une réponse vide (port muet, cas d'INV-126), ne prouve rien : pas de blocage.
- **Contrôle** : au démarrage de la gateway (modem détecté, avant les threads) et après chaque appel décroché qui a utilisé le port audio (port audio fermé, appel raccroché). Jamais pendant un appel. Seulement si le port audio USB existe.
- **Redémarrage** : canal bloqué → dès que la gateway ne voit plus aucun appel en attente (aucun appel retardé), elle journalise en CRITICAL « Canal audio bloque (CPCMREG=1) : redemarrage du modem », envoie `AT+CFUN=1,1`, laisse **30 s** au modem pour quitter puis retrouver l'USB, et sort (code 1). systemd la relance et `detect_modem_port` retrouve les ports (même principe qu'INV-126). Le port AT reste réservé pendant ces 30 s : aucun SMS ni appel n'est tenté, donc aucun essai consommé (INV-064).
- **Anti-boucle** : **au plus un redémarrage par 30 min**. L'heure du redémarrage est écrite dans `pcm_reboot_at` (répertoire de la gateway, `/opt/alarm-gateway`, dont elle est propriétaire) **avant** `AT+CFUN=1,1`. Si le canal est encore bloqué dans les 30 min : ERROR, pas de redémarrage, les appels continuent avec le repli ci-dessous. Fichier impossible à écrire : ERROR et pas de redémarrage automatique (sans trace, chaque relance de la gateway redémarrerait le modem).
- **Repli audible** : si l'injection s'arrête pendant le message (`Write timeout`) alors que l'appel continue (ni `NO CARRIER`, ni `BUSY`, ni `VOICE CALL: END`) et qu'aucune touche n'est arrivée, la gateway joue la synthèse du module (INV-132) : l'appelé entend au moins le message et peut taper 1 ou 2. Si l'appelé a raccroché, rien de plus (comme avant).
- **Pourquoi** : incident onsite-2 (Izeron) constaté le 2026-10-29. Depuis au moins le 2026-10-20, **tous les appels décrochés étaient muets**. Hors appel, `AT+CPCMREG?` répondait `1` (`0` sur le modem de Beaufort, même modèle, même firmware SIM7600M22_V2.0.1). À chaque appel, `AT+CPCMREG=0` était refusé (`ERROR`), `AT+CPCMREG=1` accepté, puis le modem cessait de lire le port audio au bout de 2-3 s (`Write timeout`). Personne n'entendait rien et l'appel finissait en `no_answer`. Seul un redémarrage du modem (`AT+CFUN=1,1`, fait à la main le 2026-10-20) le débloque, et le repli TTS ne jouait que si la voix n'avait jamais démarré. Demande du propriétaire du 2026-10-29 : « diagnostique et corrige moi ça ».
- **Couverture** : Unit `test_gateway_voice_call.py::TestPcmWedged`, `::TestVoiceFallback` ; `test_gateway_modem_health.py::TestPcmRebootAllowed`. RED avant le correctif : fonctions absentes. Le code série et les threads (`modem_gateway.py`) ne sont pas testables en unitaire (pyserial, modem réel) : validation matérielle sur onsite-2 après déploiement.

### INV-134 [H] ✅ Gateway : le SMS de détail part avant l'appel
Avant d'appeler un numéro, la gateway vérifie `/internal/sms/pending` : tant qu'un SMS **jamais tenté** (`retries = 0`) vise **ce numéro**, l'appel est différé au cycle suivant (30 s), le temps que `SmsSenderThread` l'envoie. Un SMS **déjà en échec** (`retries ≥ 1`) ne retient pas l'appel : un SMS qui échoue retarde donc l'appel d'**un cycle au plus**. Les appels vers d'autres numéros ne sont pas retardés. Si `/internal/sms/pending` ne répond pas, l'appel part quand même (ne jamais bloquer un appel d'alarme sur une panne de lecture).
- **Révision 2026-10-20** : l'ancienne règle attendait tout SMS en attente, borné seulement par INV-064 (3 échecs). En prod (alarme 818), le SMS échouait (`AT_ERROR`, modem instable) : l'appel est resté différé ~3 min et n'est parti qu'**après l'escalade**.
- **Pourquoi** : le backend crée le SMS et l'appel au même instant (INV-060), puis deux threads gateway indépendants les traitent. Un appel garde le modem jusqu'à ~45 s : si le thread des appels passait le premier, le message vocal « Consultez votre SMS » arrivait avant le SMS. Demande du propriétaire du 2026-10-14.
- **Couverture** : Unit `test_gateway_voice_call.py::TestCallDeferredForSms`.

### INV-193 [C] ✅ Plusieurs modems sur un site : chaque SMS et chaque appel part d'une seule passerelle, qui le réserve sur le serveur avant d'agir (2026-11-12)
Demande du propriétaire du 2026-11-12. Architecture cible : **chaque** machine sur site (alarm-1 à alarm-4 ; onsite-1/2 jusqu'au déménagement) a son modem, sa carte SIM et son Arduino. Un site a donc normalement **2 passerelles ou plus avec modem**, qui lisent les mêmes files (`/internal/sms|calls/pending?site=`, INV-156). Remplace la règle d'exploitation « un seul modem par site à la fois » d'INV-156/182.
- **Réserver avant d'agir** : juste avant `ATD` (appel) ou `AT+CMGS` (SMS), **port AT déjà tenu** (`at_lock`), la passerelle réserve l'élément : `POST /internal/calls/{id}/claim` ou `POST /internal/sms/{id}/claim`, corps `{"gateway_id": "<GATEWAY_ID>"}`, `X-Gateway-Key` (INV-065). Réponse `200` :
  - `{"claimed": true, "claimed_by", "claimed_until"}` → elle appelle / envoie ;
  - `{"claimed": false, "claimed_by"}` → elle **saute** l'élément : ni commande AT, ni compte rendu, une ligne de journal au niveau **debug**. Il revient au cycle suivant s'il est encore à faire.
  - `gateway_id` absent ou vide → `400` ; élément inconnu → `404`.
- **Une seule réservation gagne** : `UPDATE … SET claimed_by = :gw, claimed_until = :now + bail WHERE id = :id AND <encore à faire> AND (claimed_by IS NULL OR claimed_until < :now)`, puis le nombre de lignes modifiées (1 : gagnée, 0 : perdue). Atomique sur PostgreSQL (le second `UPDATE` attend le verrou de ligne du premier, puis réévalue sa condition sur la ligne validée) comme sur SQLite (écritures sérialisées). « Encore à faire » = ce que `/pending` remettrait : appel `called_at` vide, SMS `sent_at` vide, `retries < 3` (INV-064), alarme ni acquittée ni résolue (INV-154). `now` = `clock_now()` (INV-066).
- **Bail** (`claimed_until`), compté depuis la réservation. Elle est prise port AT tenu : l'attente du port (un appel le garde ~2 min) ne mange pas le bail.
  - **Appel : 3 min**. Pire cas du code : `ATD` 5 s, sonnerie 30 s, ouverture du canal audio ≤ 9 s, 0,8 s + message 4,9 s, attente de touche 30 s (INV-133), confirmation 2,8 s, fermeture du canal 3 s, raccrochage ≤ 6 s, contrôle du canal audio ≤ 9 s (INV-169), compte rendu HTTP ≤ 10 s : ~1 min 50 ; ~2 min 20 avec le repli TTS (INV-169, jusqu'à 64 s) ; le reste couvre la recherche d'un nouveau leader (INV-174). Mesure réelle du 2026-11-12 : 40 s.
  - **SMS : 1 min 30**. `AT+CMGF` 3 s, `AT+CSCS` 3 s, pause 2 s, invite `>` 5 s, réponse 30 s : 43 s, puis compte rendu HTTP ≤ 10 s, jusqu'à ~40 s si le leader change (INV-174).
- **`/pending` exclut** tout élément sous bail valide (`claimed_until ≥ now`), quelle que soit la passerelle qui demande.
- **Bail expiré** : l'élément revient dans `/pending` et toute passerelle peut le réserver. Une passerelle morte en plein appel ne bloque pas l'alarme plus de 3 min (SMS : 1 min 30). L'escalade n'attend de toute façon pas plus que délai + 3 min (INV-144).
- **Compte rendu** : la passerelle y joint son `gateway_id` : `POST /internal/calls/{id}/result {"result", "gateway_id"}`, `POST /internal/sms/{id}/sent {"gateway_id"}`, `POST /internal/sms/{id}/error {"error", "gateway_id"}`. Effet sur l'élément inchangé (résultat, `called_at` / `sent_at`, `retries`) : `no_answer` compte comme passé et ne repart pas ; seul `error` (appel) ou l'échec (SMS) est rejoué, jusqu'à 3 essais (INV-148, INV-144, INV-064).
  - Posté par la passerelle **qui tient la réservation** : la réservation est levée. Un `error` est aussitôt de nouveau en file.
  - Posté par **une autre** passerelle, ou sans `gateway_id`, alors qu'une réservation existe : **accepté** quand même (l'appel ou le SMS a bien eu lieu ; le refuser ferait rejouer un appel décroché ou perdre un acquittement par touche 1). **La réservation reste** : son titulaire est peut-être encore en ligne, et un `error` étranger ne doit pas remettre l'élément en file pendant son appel. Le serveur journalise un **WARNING** qui nomme les deux passerelles : signe d'un double envoi (passerelle pas encore à jour, ou bail dépassé).
- **Versions mélangées**, le temps qu'`alarm-gateway-sync` déploie la nouvelle passerelle partout (INV-141/182) :
  - passerelle **pas encore à jour** : `/pending` et les comptes rendus sans `gateway_id` (`/sent` sans corps) restent acceptés, au même format. Elle ne réserve rien : elle peut encore doubler un SMS ou un appel, comme avant, jusqu'à sa mise à jour. Une passerelle à jour, elle, ne prend jamais un élément réservé.
  - passerelle à jour face à un serveur **pas encore à jour** (`/claim` inconnu : `404`) ou injoignable au moment de réserver : elle appelle / envoie quand même, comme avant. Ne jamais bloquer un SMS ou un appel d'alarme sur une panne de réservation (même principe qu'INV-134). Seule une réponse `claimed: false` fait sauter l'élément.
- **Journal, pas d'audit** : réservation perdue → une ligne **debug** côté serveur et une côté passerelle ; aucun `AuditEvent`.
- **Migration** `_migrate_queue_claims` : colonnes nullables `claimed_by VARCHAR` et `claimed_until TIMESTAMP` sur `sms_queue` et `call_queue`, SQLite et PostgreSQL, idempotente. Aucune donnée touchée : les lignes existantes sont non réservées.
- **Limite** : INV-134 (SMS avant l'appel) reste garanti sur une même passerelle, qui tient le port AT pendant l'envoi du SMS. D'une passerelle à l'autre, un SMS réservé ailleurs n'apparaît plus dans `/internal/sms/pending` : l'appel peut partir pendant que le SMS part de l'autre modem, à quelques secondes près.
- **Pourquoi** : incident du 2026-11-12, alarme 1231, site Izeron, trois passerelles avec modem (onsite-2, alarm-3, alarm-4). L'appel 949 est composé par alarm-3 à 17:11:52 (heure locale). Le propriétaire décroche, puis raccroche sans touche : `no_answer` posté à 17:12:32. Entre-temps, alarm-4 (modem prêt à 17:11:56) a lu `/internal/calls/pending` : l'appel n'était marqué qu'au compte rendu, il y figurait encore. alarm-4 l'a recomposé à 17:12:49 (`no_answer` à 17:12:53). Un troisième appel est très probablement venu d'onsite-2. Ligne finale `retries = 3`, `result = no_answer` : **3 appels pour 1 appel en file**, contre INV-133/148 (`no_answer` compte comme passé et n'est pas rejoué).
- **Couverture** : Integration `tests/integration/test_queue_claim_inv193.py`, appels et SMS : deux passerelles lisent la même file, une seule réservation gagne, la perdante ne voit plus l'élément (RED avant le correctif : alarm-4 voyait encore l'appel réservé par alarm-3) ; file vide sous bail ; bail expiré (appel 3 min, SMS 1 min 30) → de nouveau en file, réservable par une autre ; `error` / échec SMS : réservation levée, rejoué jusqu'à 3 essais puis abandonné ; `no_answer` / envoyé jamais rejoué, même après le bail ; compte rendu d'une autre passerelle accepté, réservation gardée, WARNING ; passerelle d'avant (sans réservation ni `gateway_id`, `/sent` sans corps) inchangée ; alarme résolue → réservation refusée ; `400` / `401` / `404` ; migration d'une base d'avant. Unit `tests/unit/test_gateway_claim.py` (vrais `SmsSenderThread` / `CallSenderThread`, port série et backend simulés) : réservation, port AT tenu, avant `ATD` / `AT+CMGS`, `gateway_id` dans le compte rendu ; réservation perdue → aucune commande AT, aucun compte rendu, une ligne debug ; `/claim` sans réponse → appel / SMS quand même. E2E tier 3 `tests/test_sms_voice.py::TestQueueClaimConcurrency` (PostgreSQL du cluster de CI : 8 passerelles réservent le même appel / SMS au même instant, une seule gagne). Joué le 2026-11-12 sur un PostgreSQL 16 jetable (backend local, faux Patroni) : 5 passages verts ; un `UPDATE` non conditionnel (lire puis écrire) y donne 2 à 5 gagnantes sur 8, le test le voit. Le fichier d'intégration passe aussi sur ce PostgreSQL. **Non vérifié sur le matériel** (plusieurs modems réels d'un même site).

### INV-135 [M] ✅ Web : rafraîchissement par WebSocket, un panneau ne se recharge que si ses données changent
L'UI web ouvre `/api/ws` et envoie `{token, paths}`. Le nœud rejoue ces GET en interne toutes les 3 s et n'envoie `{"changed": path}` qu'à la connexion (sync initiale) puis quand la réponse change. Token invalide/expiré → fermeture `4401` → logout. Coupure → reconnexion espacée (3 s → 30 s), une seule ligne console. Filet de sécurité : resync complète toutes les 60 s (stats exclues tant que le live est ouvert).
- **Pourquoi** : l'autorefresh 5 s rechargeait tout (dont le graphe des stats) en permanence et, backend en 503, inondait la console de ~10 erreurs toutes les 5 s. Pas de bus inter-nœuds : chaque nœud lit sa propre DB, donc ça marche aussi sur un replica.
- **Couverture** : `tests/test_live_ws.py` (notif initiale, silence sans changement, notif après changement, 4401, paths hors `/api/` ignorés).

### INV-136 [M] ✅ Web : schéma d'escalade en temps réel de l'alarme courante (onglet Escalade)
`GET /api/alarms/active/escalation` (tout utilisateur connecté) décrit l'escalade de l'alarme non résolue en cours :
- `alarm` : `null` sans alarme, sinon `{id, title, status, assigned_user_id, escalation_count, original_created_at, created_at, acknowledged_by, acknowledged_by_name, acknowledged_at, suspended_until, ack_expired}`. `ack_expired` = alarme redevenue `active`/`escalated` alors qu'elle avait été acquittée (acquittement oublié, INV-016).
- `escalation_delay_minutes`, `sms_call_delay_minutes`, `next_escalation_at` (= `created_at + escalation_delay`, `null` si l'alarme est acquittée).
- `steps` : une entrée par position de la chaîne, dans l'ordre : `{position, user_id, name, has_phone, is_online, role, notified_at, contact_at, sms, call, acked}`.
  - `role` : `current` (l'utilisateur assigné, celui qu'on cherche à joindre), `notified` (déjà notifié, sonne toujours, INV-012), `upcoming` (pas encore notifié).
  - `contact_at` = `notified_at + sms_call_delay` (moment du SMS + appel, INV-060).
  - `sms` : `none` (pas notifié), `no_phone`, `scheduled` (délai pas encore atteint), `pending`, `sent`, `failed` (≥ 3 essais).
  - `call` : `none`, `no_phone`, `scheduled`, `pending`, ou le résultat gateway (`ack_dtmf`, `ack_sms`, `no_answer`, `busy`, `error`, `escalate`).
  - `acked` : c'est cet utilisateur qui a acquitté.
- La réponse ne contient **aucun champ qui change sans événement** (pas de « secondes restantes ») : le WebSocket INV-135 ne notifie que sur un vrai changement, les comptes à rebours sont calculés par le navigateur.

Le web affiche `#escalationLive` en tête de l'onglet Escalade (`data-state` = `none` | `active` | `acknowledged`). Une ligne par utilisateur (`.esc-lane`, `data-user`, `data-role`), chacune avec les étapes Notification → attente SMS/appel → SMS + appel → résultat → escalade au suivant (`.esc-stage`, `data-stage`, `data-status` = `todo` | `doing` | `ok` | `ko`). Couleurs : gris à venir, bleu pulsé en cours, vert acquitté/OK, rouge échec, orange acquittement expiré ou escalade. Comptes à rebours vers le SMS/appel et vers l'escalade suivante. Mise à jour en temps réel via `/api/ws`.
- **Pourquoi** : demande du propriétaire du 2026-10-16. Pendant une alarme, voir d'un coup d'œil qui est censé être joint, à quelle étape on en est, et ce qui a échoué (appel sans réponse, acquittement oublié), sans fouiller le journal.
- **Couverture** : E2E `tests/test_escalation_live.py` ; Playwright `test_frontend.py::TestEscalationLiveDiagram`.

### INV-137 [C] ✅ Secours 4G : aucun client QMI fui, route posée quelle que soit la passerelle opérateur
`scripts/4g-standby.sh` (déployé en `/opt/alarm/4g-standby.sh`) :
1. **Chaque client WDS alloué est libéré** : un démarrage raté, une IP absente ou une route refusée libère le CID (`--wds-noop --client-cid`). Une session tombée ou l'arrêt du service la coupe et libère le CID (`--wds-stop-network --client-cid`).
2. **La route par défaut 4G est posée `onlink`** (adresse en `/32`, interface vidée avant), même quand la passerelle n'est pas dans le même `/30` que l'adresse. Un refus de route est journalisé comme un échec, jamais comme « lien 4G UP ».
3. **Connexion par défaut déjà ouverte** : si l'opérateur refuse `apn=…` avec `multiple-connection-to-same-pdn-not-allowed` (cas après un redémarrage du modem : l'attache LTE a ouvert la connexion par défaut), le CID est libéré et la session est démarrée sur le profil d'attache (`3gpp-profile=1`). Constaté sur alarm-1 le 2026-10-19 : sans ça, la 4G ne remontait jamais après `AT+CFUN=1,1`.
- **Pourquoi** : incident onsite-2 du 2026-10-19.
  - (1) Depuis le 14/10, la session 4G tombait (38 fois). Chaque reconnexion ratée, toutes les 15 s, allouait un CID jamais libéré (1 356 échecs en 19 h, CID 89 → 124 → …). Le modem a saturé, ne répondait plus en AT, puis il est tombé du bus USB à 01:06 et à 09:28 (récupéré par le watchdog, e-mail « droppé puis récupéré »). Contact sec, SMS, voix et 4G sont restés HS pendant ces fenêtres.
  - (2) Ensuite, l'opérateur a attribué `10.133.126.63` avec la passerelle `.64` (hors du `/30`). Le noyau a refusé la route (`Nexthop has invalid gateway`) alors que le script annonçait « lien 4G UP » : le secours 4G était inopérant sans alerte.
- **Couverture** : Unit `tests/unit/test_4g_standby.py` (faux `qmicli`/`ip`). RED sur le script d'origine : 5 CID fuis sur 6, route sans `onlink`.

### INV-140 [C] ✅ Secours 4G : bascule automatique quand la box est allumée mais sans internet
Sur un nœud qui a une route 4G (`dev ww*`, posée par `alarm-4g-standby`, INV-137), `alarm-wan-failover` teste internet **par la box** toutes les **10 s** (`ping -I <interface box>` vers le cloud `51.210.105.102`, `1.1.1.1`, `9.9.9.9` ; un seul succès suffit).
- **3 échecs consécutifs** par la box **et** la 4G répond → la route par défaut de la box passe en **métrique 500** (derrière la 4G, métrique 300) : tout le trafic sort par la 4G.
- **3 succès consécutifs** par la box (testée sur son interface même rétrogradée) → la route de la box reprend sa priorité d'origine (métrique 0).
- La box n'est **jamais** rétrogradée si la 4G ne répond pas (pas de bascule vers un lien mort), ni sur un nœud sans 4G (onsite-1 : ne fait rien).
- À l'arrêt du service (ou au démarrage s'il trouve une box rétrogradée par une exécution précédente), la route de la box est remise en priorité : le service ne laisse jamais la box rétrogradée derrière lui.
- **Pourquoi** : la route par défaut vers la box (`192.168.1.1`, statique `onlink`) ne disparaît que si le câble Ethernet du nœud est coupé. Box éteinte ou sans internet, le port reste UP : le trafic continuait vers une box morte et la 4G (métrique 300) ne prenait **jamais** le relais. Constaté le 2026-10-19 en relisant les routes réelles d'onsite-2 ; le smoketest de juin simulait la panne en retirant la route à la main. Demande du propriétaire du 2026-10-19.
- **Couverture** : Unit `test_wan_failover.py` ; validation matérielle sur alarm-1 (ICMP coupé sur l'interface box → bascule 4G → retour).

### INV-141 [C] ✅ Déploiement auto de la partie hôte (scripts + units `infra/onsite`) au commit promu
Sur chaque nœud on-site, `alarm-host-sync.timer` (toutes les 5 min, root) lit le commit promu (label `org.opencontainers.image.revision` de `alarm-backend:stable`, déjà tirée par `alarm-cd-pull`). S'il diffère du dernier appliqué : `/opt/alarm` passe en checkout détaché sur ce commit (en tant que propriétaire du clone), puis `infra/onsite/install.sh --auto` installe les scripts/units qui ont changé et **redémarre les seules units concernées**. Seul un commit passé en CI et promu `:stable` est déployé, comme pour les images.
- Une **modification locale suivie** n'est écrasée que si le commit cible la contient déjà : fusion à 3 voies (base = HEAD, local, cible) identique à la cible, ou à défaut chaque bloc ajouté localement présent tel quel dans la cible (suppression pure : absente de la cible). Sinon : erreur journalisée nommant le fichier, **rien n'est touché**. Cas réel du 2026-10-20 sur onsite-1/2 : `docker-compose.yml` avec le correctif de bind `ADVERTISE_HOST` posé à la main (déjà dans master, voisinage modifié depuis → conflit `merge-file`, d'où le second critère) et `.env.prod.node{1,2}` avec un bloc SMTP jamais remonté (remonté dans le repo). Le premier bootstrap avait refusé ; sans la remontée du SMTP, l'écraser aurait coupé les emails d'alerte des nœuds on-site.
- `--auto` ne touche **pas** au code gateway (déployé et redémarré par `alarm-gateway-sync` depuis l'image), sauf pour le poser s'il est absent (premier passage, INV-182). **Révisé le 2026-11-05 (INV-182)** : la passerelle et la pile modem (`alarm-4g-standby`, `alarm-modem-rat`, `alarm-modem-watchdog`) sont installées et actives sur **chaque** machine sur site, le matériel pouvant être branché sur n'importe laquelle ; sans modem elles attendent sans échec, et le watchdog n'agit que sur un modem déjà vu (INV-184). (Avant : pile activée seulement là où existait le user `alarm-gateway`, désactivée ailleurs — constat du 2026-10-20 : elle tournait sur onsite-1 sans modem, avec un watchdog susceptible d'alerter « modem absent » à tort.)
- `install.sh` installe aussi `alarm-gateway-sync` et `alarm-host-sync` (scripts + timers) : après **une** exécution manuelle par nœud (bootstrap), la partie hôte se met à jour seule.
- **Dossier d'état `/var/lib/alarm/cd` (amendé le 2026-11-09)** : il appartient à `alarm`. Seules les units qui tournent en `alarm` (`alarm-cd-pull`, et sur NODE3 `alarm-cd-orchestrate`, `alarm-chaos`) le déclarent en `StateDirectory=alarm/cd` ; les units root (`alarm-host-sync`, `alarm-gateway-sync`) ne le déclarent pas et le créent elles-mêmes au besoin (`mkdir -p`). Pourquoi : systemd redonne **récursivement** un `StateDirectory` au `User=` de l'unit qui démarre ; avec des units root et `alarm` sur le même dossier, chaque démarrage d'une unit root repassait tout en `root` (644) : `alarm-cd-pull` échouait par moments (« impossible de faire un touch '/var/lib/alarm/cd/digests.tsv': Permission non accordée », vu sur onsite-1 et onsite-2 le 2026-11-08/09), et sur NODE3 l'orchestrateur pouvait perdre l'écriture de son état ou de son verrou en plein déploiement.
- **Pourquoi** : le 2026-10-19, la voix française (INV-132) n'est pas arrivée en prod : la nouvelle version d'`alarm-gateway-sync` (qui copie `voice/*.wav`) n'avait jamais été réinstallée sur onsite-2, posée à la main le 13/10. Toute la partie hôte (`4g-standby`, watchdog modem, bascule INV-140…) avait le même trou. Demande du propriétaire du 2026-10-19 : « que ça déploie automatiquement sur les deux on-site ».
- **NODE3 (étendu le 2026-11-13, décision du propriétaire)** : NODE3 (cloud) a aussi `alarm-host-sync`, avec le drop-in `alarm-host-sync.service.d/node3.conf` qui fixe `HOST_INSTALLER=infra/node3/install.sh`. Même checkout au commit promu, mêmes règles sur les modifications locales ; puis `infra/node3/install.sh --auto` (au lieu d'`infra/onsite/install.sh`) installe **seulement les fichiers de NODE3 qui ont changé** : `alarm-cd-orchestrate`, `alarm-cd-canary.sh`, `alarm-cd-rollback.sh`, `alarm-cd-pull`, `alarm-db-backup`, `/opt/alarm/send-alert-email.py`, `alarm-host-sync` lui-même, les units (`.service` + `.timer`) de `alarm-cd-orchestrate`, `alarm-cd-pull`, `alarm-db-backup`, `alarm-db-restore-test`, `alarm-host-sync`, et le drop-in. **Jamais** la pile modem, la passerelle ni la sudoers du site.
  - `--auto` **n'active ni ne démarre aucun timer** : un timer arrêté pour une maintenance (ex. recréation d'etcd, CD_DESIGN §9sexies) le reste. Il ne **relance qu'un timer actif dont l'unit a changé** ; `daemon-reload` seulement si une unit a changé. Un fichier inchangé n'est pas réécrit.
  - **Bootstrap**, une fois à la main : `sudo bash /opt/alarm/infra/node3/install.sh` (sans `--auto`) installe tout, `daemon-reload`, et active les 5 timers (`enable --now`).
- **Jamais pendant un passage CD** : si le verrou de l'orchestrateur `$STATE_DIR/orchestrate.lock` existe (NODE3 seul : orchestrateur et runner de pannes contrôlées, INV-178), `alarm-host-sync` le prend **avant le checkout**, en attendant au plus `LOCK_WAIT` = 480 s (sous le `TimeoutStartSec` de 600 s) ; toujours pris → exit 1 « passage CD en cours », **rien n'est touché**, nouvel essai au tick suivant. Le verrou est ouvert **en lecture** : root ne crée jamais ce fichier, qui doit rester à `alarm`. Sur le site, le fichier n'existe pas : sans effet.
  - **Pourquoi** : constat du 2026-11-13 : `/opt/alarm` de NODE3 était figé au 27/10 (97 commits de retard, HEAD détaché posé à la main) ; le canary de node3 relançait ses conteneurs avec ce `docker-compose.yml` et ce `.env.prod.node3` (backend sans node6/node7 dans `PEER_TEST_URLS`), et chaque PR touchant le CD ou la sauvegarde exigeait « réinstaller à la main sur NODE3 ». Le verrou : remplacer l'orchestrateur, le canary ou le compose de node3 en plein passage mélangerait deux versions.
- **Couverture** : Unit `test_alarm_host_sync.py` (vrai dépôt git temporaire : modifs déjà dans la cible → déployé ; modif absente de la cible → refus, rien touché ; clone propre → déployé ; `HOST_INSTALLER` → cet installeur en `--auto`, pas celui du site ; verrou de l'orchestrateur pris → exit 1, HEAD inchangé, aucun installeur ; verrou libre → déployé et fichier du verrou inchangé ; units réelles d'`infra/onsite/systemd` : `StateDirectory=alarm/cd` déclaré seulement par des units `alarm`, les scripts root créent le dossier). Unit `test_node3_install.py` (vrai `infra/node3/install.sh` dans une racine temporaire, faux `systemctl`) : bootstrap → les 18 fichiers aux bons modes, drop-in pointant vers un installeur du dépôt, `daemon-reload`, `enable --now` des 5 timers ; `--auto` sans changement → aucun fichier réécrit, aucun `daemon-reload`, aucun démarrage ; `--auto` avec un script et un timer changés → réécrits, `daemon-reload`, timer actif relancé, timer arrêté laissé arrêté, jamais d'`enable`/`start`. Validation matérielle alarm-1 (site) ; NODE3 : à valider au bootstrap.

### INV-138 [H] ✅ Onglet Tests en prod : 4 actions réelles, admin, sans rien détruire
En prod (`/api/test/*` coupé, INV-076), l'onglet Tests propose, **en plus** de « Envoyer une alarme », des actions qui passent par de **vraies routes** réservées aux admins (`403` sinon) et tracées dans l'audit :
- **Escalader au suivant** — `POST /api/alarms/escalate-now` : chaque alarme `active`/`escalated` passe immédiatement à l'utilisateur suivant de la chaîne (même effet qu'un palier d'escalade : notification, `escalation_count + 1`, timer remis à zéro, FCM aux notifiés). Event `alarm_escalated`.
- **Me mettre hors ligne (5 min)** — `POST /api/devices/me/go-offline` : **seul l'admin qui clique** passe hors ligne et ses heartbeats sont refusés (`503`) pendant **5 min**. Les autres utilisateurs ne sont pas touchés. Event `test_go_offline` (audité, `user_id` = l'admin).
- **Couper mes heartbeats** — `POST /api/devices/me/heartbeat-pause` : bascule. Tant qu'elle est active (au plus **5 min**, puis fin automatique), les heartbeats de l'admin sont refusés (`503`) ; le watchdog le marque hors ligne au délai habituel. Un second clic rétablit les heartbeats. Event `test_heartbeat_pause` (audité, `details.paused` = `true` / `false`).
- **Résoudre les alarmes actives** — `POST /api/alarms/{id}/resolve` sur chaque alarme active. **Rien n'est supprimé**, la chaîne d'escalade et la configuration restent intactes.
En dev/CI, les boutons de simulation `/api/test/*` restent inchangés.
- **Audit (amendé le 2026-10-30)** : `test_go_offline` et `test_heartbeat_pause` font partie de `AUDITABLE_EVENTS` et s'écrivent dans `audit_events` : après le clic, `GET /api/audit/?event_type=test_go_offline` (resp. `test_heartbeat_pause`) renvoie au moins un event portant l'`user_id` de l'admin. Constat du 2026-10-30 : ces deux events étaient écrits dans les logs mais jamais persistés dans l'audit, contrairement à ce que promettait cet invariant.
- **Pourquoi** : PR #219 avait masqué ces boutons en prod (ils appelaient `/api/test/*`). Le propriétaire veut les retrouver en prod (2026-10-19), mais la remise à zéro de test supprime tout l'historique et remplace la chaîne réelle, et les simulations de test touchent tous les utilisateurs. Décision du propriétaire : versions sûres, limitées à son propre compte et bornées dans le temps.
- **Couverture** : Integration `test_tests_tab_prod_inv138.py` (dont `test_go_offline_is_audited`, `test_heartbeat_pause_is_audited`, 2026-10-30) ; Playwright `test_frontend.py::TestTestsTabInProd`.

### INV-139 [M] ✅ Web : chaque action affiche un retour visuel (snackbar succès ou erreur)
Toute écriture déclenchée depuis le web (POST/PUT/PATCH/DELETE via `api()` : enregistrer, supprimer, envoyer, résoudre, boutons de l'onglet Tests…) affiche une snackbar `#snackbar` en bas de l'écran : **verte** « Action effectuée » si la requête réussit, **rouge** avec le message d'erreur du serveur (`detail`) sinon. Une seule snackbar à la fois (la nouvelle remplace l'ancienne), disparition après 3 s (succès) ou 6 s (erreur), annoncée aux lecteurs d'écran (`role="status"`). Les lectures (GET) n'en affichent pas.
- **Pourquoi** : demande du propriétaire du 2026-10-19 : après un clic, rien n'indiquait si l'action avait réussi.
- **Couverture** : Playwright `test_frontend.py::TestActionFeedback`.

### INV-150 [M] ✅ Web : utilisable sur téléphone (2026-10-22)
À 375 px de large (téléphone), **aucun onglet** ne fait défiler la page horizontalement, même avec une alarme en cours. Tous les onglets restent atteignables (la barre d'onglets défile sur elle-même), l'en-tête (dont « Déconnexion ») tient dans l'écran, les formulaires passent à la ligne et les tableaux larges défilent à l'intérieur de leur carte. L'affichage sur ordinateur ne change pas.
- **Complément du 2026-10-29** (plan validé par le propriétaire) :
  - **Tablette** : à 768 et 1 024 px non plus, aucun onglet ne fait défiler la page (un contenu trop large défile dans sa carte).
  - **Sous 700 px**, les tableaux de l'alarme en cours (tableau de bord), des Utilisateurs, des Alarmes, du Log et du Cluster deviennent des **cartes empilées** : une carte par ligne, le nom de la colonne devant chaque valeur. Le bouton « Résoudre » de l'alarme en cours est dans l'écran sans défilement horizontal.
  - **Sous 700 px**, tout bouton fait au moins **44 px** de haut (« Déconnexion », petits boutons et boutons de la chaîne compris) ; champs et listes en **16 px** (pas de zoom automatique d'iOS). Les graphes sont moins hauts.
  - Aucun champ n'a une largeur fixe qui dépasse l'écran ; les groupes champ + « Sauvegarder » passent à la ligne. Les deux colonnes de la chaîne d'escalade passent l'une sous l'autre.
  - Chaque membre de la chaîne a des boutons **▲ / ▼** qui le montent ou le descendent (le glisser-déposer ne marche pas au doigt) ; comme le glisser-déposer (conservé), « Sauvegarder » enregistre l'ordre.
  - La barre d'onglets montre qu'il reste des onglets à droite (bord estompé).
- **Pourquoi** : demande du propriétaire du 2026-10-22. Mesuré avant correction : page de 1 039 px de large sur un écran de 375 px (barre des 9 onglets, en-tête, tableaux, formulaires). Le 2026-10-29 : à 768 px, les onglets Alarmes (784 px) et Log (900 px) débordaient ; à 375 px, « Résoudre » était à x = 543 px, au bout d'un tableau à faire défiler.
- **Couverture** : Playwright `test_frontend.py::TestMobileLayout` (375, 768, 1 024 px ; « Résoudre » dans l'écran), `::TestEscalationDragDrop::test_move_down_button_reorders_chain_for_save` (joués en local, hors CI).

### INV-165 [H] ✅ Web : un non-admin ne voit que le suivi, pas l'administration (2026-10-29)
Après connexion, le web lit `user.is_admin` dans la réponse de `/api/auth/login`. Pour un utilisateur **non admin** :
- **Onglets visibles** : Tableau de bord, Escalade (lecture seule : schéma d'escalade en cours, INV-136), Alarmes, Statistiques.
- **Onglets masqués** : Utilisateurs, Tests, Log, Disponibilité, Cluster.
- **Actions masquées** : configuration de l'escalade (chaîne, délai, email d'alerte), « Résoudre », « Envoyer une alarme », et tout ce que portent les onglets masqués (« Supprimer », « Réinitialiser », actions de l'onglet Tests).
- **Aucun GET réservé aux admins** n'est appelé pour lui (`/api/config/system`, `/api/audit/`, `/api/stats/connectivity`, `/api/chaos`), ni surveillé par le WebSocket live (INV-135), ni relancé par la resynchro de sécurité de 60 s (constat du 2026-11-10 : `GET /api/chaos` partait toutes les 60 s, `403`) : pas de `403` dans la console. Il reste connecté après le chargement initial.
- **`401` seul déconnecte** (jeton expiré ou invalide). Un `403` (droit manquant) ne déconnecte pas : l'action échoue avec « Réservé aux administrateurs » (snackbar rouge pour une écriture, INV-139).
- Un **admin** voit tout, comme avant. Si la session précédente avait laissé un onglet admin ouvert, un non-admin revient au tableau de bord.
- **Masquer n'est pas une sécurité** : chaque route admin refuse elle-même un non-admin (`403`) côté serveur (`/api/alarms/{id}/resolve`, `GET /api/config/system`, etc., PR backend #274 du même plan).
- **Pourquoi** : plan validé par le propriétaire le 2026-10-29. Les opérateurs d'astreinte doivent pouvoir suivre une alarme depuis le web sans voir ni toucher la configuration.
- **Couverture** : Playwright `test_frontend.py::TestRoleBasedUI` (joué en local, hors CI).

### INV-166 [L] ✅ Web : pied de page avec la version du serveur et le nœud (2026-10-29)
Chaque onglet affiche en pied de page `Version <version> · nœud <local_node> (<Lieu>)` (format du propriétaire, 2026-10-29 : « uniquement le nœud et la bonne version ») : `version` (numéro de release, ex. `1.6.1`, INV-164) vient de `/health` du nœud qui sert la page, `local_node` et `local_location` de `/api/cluster` (public, lieu au format INV-161). Version absente (backend plus ancien) → `?` ; nœud inconnu → `?` ; lieu inconnu → pas de parenthèses.
- **Pourquoi** : plan du 2026-10-29. Savoir d'un coup d'œil quelle version tourne et quel nœud répond, sans ouvrir l'onglet Cluster (réservé aux admins, INV-165).
- **Couverture** : Playwright `test_frontend.py::TestFooter` (joué en local, hors CI).

### INV-124 [H] ✅ Corps du SMS d'alarme : instruction d'acquittement + lien supervision
Le corps du SMS d'alarme métier (escalade, `_enqueue_sms_for_user`) doit (a) instruire
l'opérateur d'acquitter en **répondant `1`** au SMS, et (b) contenir le **lien de supervision
`https://supervision.charlesmurgat.com`**.
- **Pourquoi** : la gateway sait recevoir l'ack par SMS (`SmsReceiverThread` : corps `1`/`ok`/`oui`/`ack`
  → `POST /internal/alarms/active/ack-by-phone`, cf `gateway/modem_gateway.py`), **mais** le SMS
  sortant ne le disait pas → l'opérateur qui ne connaît pas le système ne sait pas qu'il peut
  acquitter par réponse SMS, et n'a aucun lien pour rejoindre la supervision. Décision propriétaire
  2026-06-17. Ne concerne **pas** les SMS de ping isolation (INV-067, body `[ALARME-MURGAT-PING]`).
- **Texte (amendé le 2026-11-12, demande du propriétaire)** : `ALARME <Site>: <titre>` (INV-155 ;
  `ALARME:` sans site), retour à la ligne, `Répondez 1 pour acquitter ou https://supervision.charlesmurgat.com`.
  « Répondez » reprend son accent. Le titre est repris tel quel (« Déclenchement contact sec — Beaufort ») :
  la passerelle ramène le texte à l'alphabet GSM (ci-dessous, le tiret long devient « - »).
  Dédoublonnage INV-062 intact (body déterministe par le titre et le site).
- **Contrainte encodage (amendée le 2026-11-12, demande du propriétaire)** : le SMS reste en
  alphabet GSM 7 bits, 160 caractères (pas UCS-2, 70 caractères). Le SMS d'alarme contact sec du site
  au nom le plus long (Beaufort) fait 120 caractères : un seul SMS ; un nom de site tient jusqu'à 28
  caractères. Chaque SMS envoyé par la passerelle modem (`gateway/modem_gateway.py` : file
  `/internal/sms/pending`, donc alarme, ping INV-067 et test ; SMS d'alerte backends INV-171) suit la
  séquence `AT+CMGF=1`, `AT+CSCS="GSM"`, `AT+CMGS="<numéro>"`, puis le texte en codes GSM 03.38, **un
  octet par caractère**, puis Ctrl-Z (0x1A).
  - **Table de base GSM 03.38** : chacun de ses caractères part avec son code (é 0x05, è 0x04, ù 0x06,
    ì 0x07, ò 0x08, à 0x7F, É 0x1F, Ç 0x09, @ 0x00, £ 0x01, $ 0x02, ¤ 0x24, ¡ 0x40, ¿ 0x60, Ä 0x5B,
    ü 0x7E, retour à la ligne 0x0A…). Lettres sans accent, chiffres, ponctuation courante : inchangés.
  - **Tout autre caractère** est remplacé par le plus proche de la table de base : accent retiré
    (ê→e, â→a, î→i, ô→o, û→u, ë→e, ï→i, À→A, Ê→E…), ç→Ç (0x09), œ→oe, Œ→OE, tirets — et –→-,
    apostrophes ’ ‘→', guillemets « » “ ”→", espaces insécables→espace, …→..., tabulation→espace ;
    sans équivalent (emoji, caractère de contrôle…)→`?`.
  - **Table d'extension non utilisée** (`€ [ ] { } \ ^ ~ |`, normalement ESC 0x1B + code) : en mode
    texte, ESC annule la saisie du SMS (3GPP TS 27.005 §3.5.1), le SMS ne partirait pas. Remplacés :
    €→EUR, [ et {→(, ] et }→), \ et |→/, ^→', ~→-.
  - **Jamais** d'octet 0x1A (Ctrl-Z, fin du SMS) ni 0x1B (ESC) dans le texte : Ξ (0x1A dans la table)
    et un Ctrl-Z ou un ESC présent dans le titre deviennent `?`.
  - `@` part en 0x00 (table de base) : le port série l'écrit tel quel.
  - `AT+CSCS="GSM"` sans `OK` → échec `CSCS_FAILED` (comme `CMGF_FAILED`, essai compté INV-064), aucun
    `AT+CMGS`. SMS d'alerte backends : réponses non contrôlées, comme avant.
  - **Pourquoi** : SMS d'alarme reçu le 2026-11-12 sur le téléphone du propriétaire :
    « … DC(clenchement contact sec … » au lieu de « Déclenchement ». La passerelle écrivait le texte en
    UTF-8 (é = C3 A9) sans `AT+CSCS` ; le modem attend l'alphabet GSM. L'ancienne règle (« pas d'accent
    dans la partie statique ») ne protégeait pas le titre.
  - **Validé sur le vrai SIM7600 le 2026-11-12** : `AT+CMGF=1`, `AT+CSCS="GSM"`, `AT+CMGS`, texte en
    codes GSM (é=0x05…), Ctrl-Z → le téléphone du propriétaire affiche « Déclenchement … Répondez … à è ù é É ».
    Non essayés sur le modem : `@` (0x00), les lettres grecques.
- **Couverture** : E2E `TestSmsCallTimer::test_sms_body_has_ack_instruction_and_supervision_link`.
  Integration `test_multisite_inv155_156.py::test_sms_and_calls_carry_site_and_pending_filters_by_site`
  (« Répondez ») et `test_dry_contact_sms_body_fits_one_gsm_sms` (texte exact du contact sec de
  Beaufort, 160 caractères au plus). Unit `test_gateway_sms_gsm7.py` : table de base complète,
  remplacements, ni 0x1A ni 0x1B émis, textes d'alarme ; octets écrits sur un faux port série par
  `SmsSenderThread._send_sms` et `HealthMonitorThread._send_alert_sms` (séquence `CMGF`, `CSCS="GSM"`,
  `CMGS`, texte GSM + Ctrl-Z ; `CSCS_FAILED`). RED avant le correctif : texte écrit en UTF-8, pas de
  `AT+CSCS`.

### INV-065 [C] ⚠️ Gateway key obligatoire
`/internal/sms/*` et `/internal/calls/*` nécessitent header `X-Gateway-Key` valide.
- **Pourquoi** : sécurité — ces endpoints exposent les numéros de tel.
- **Couverture** : E2E `TestSmsAndHealth::test_sms_pending_requires_gateway_key` + `test_sms_pending_wrong_key_returns_401`.

### INV-067 [C] 🐛 SMS ping périodique au pos 1 d'astreinte tant que son heartbeat est KO (2026-06-03)

Pendant fortifier la sonnerie d'isolation locale (cf `android/INVARIANTS.md` INV-ANDROID-308 nouvelle version 2026-06-03), le backend doit envoyer un SMS `[ALARME-MURGAT-PING] $iso_timestamp` au numéro de téléphone du user en **position 1** de la chaîne d'escalade, **dès** que son heartbeat HTTP est KO depuis ≥ 30 s (≈ 1 cycle watchdog INV-041), puis **toutes les 2 minutes** tant que ce heartbeat reste KO.

**Mécanisme** :
1. À chaque tick d'`escalation_loop` (cf section 4) ou tick dédié, on consulte `User.last_heartbeat` du pos 1.
2. Si `now - last_heartbeat >= 30s` ET pas de ping envoyé depuis ≥ 2 min (`SystemConfig.last_ping_sent_at_user_<id>` ou colonne `User.last_ping_sent_at`, à choisir), insertion dans `SmsQueue` :
   - `to_number = pos1.phone_number`
   - `body = f"[ALARME-MURGAT-PING] {clock_now().isoformat()}"`
   - `alarm_id = NULL` (ce n'est pas un SMS d'alarme métier)
   - `is_ping = TRUE` (nouvelle colonne ; permet anti-pollution stats SMS et filtrage côté `INV-060` qui ne doit PAS considérer ces SMS comme notifications d'alarme).
3. La gateway SIM7600 envoie via son flux normal (cf INV-060).
4. Le DLR (Delivery Report Orange/Sosh) est reçu côté gateway → upsert dans une table dédiée (`PingDeliveryReport(user_id, sent_at, delivered_at, status_code, retries)`) pour audit + diagnostic. Pas de logique métier sur le DLR côté backend dans cette V1 — la décision est entièrement côté app (réception SMS ou non).
5. Quand `User.is_online` repasse `True` (heartbeat 2xx, cf INV-040), reset `last_ping_sent_at` à NULL.

**Pourquoi** : sans cet envoi serveur, l'app Android ne peut PAS distinguer "isolation totale" de "backend juste down" — elle sonnera dès 5 min de heartbeat KO peu importe la cause. INV-067 garantit qu'un SMS atteint le tél (s'il est joignable par cellulaire) dans la fenêtre de 5 min côté client, ce qui désamorce la sonnerie locale dans tous les cas où l'opérateur reste joignable par SMS.

**Marge de timing** : le client a un timeout de 5 min après `heartbeatLostSince`. Le backend tente d'envoyer dès 30 s + relance toutes les 2 min → au pire 1 ping dans la fenêtre [30s, 4min30s], le SMSC Orange peut prendre jusqu'à ~1 min en routage normal. Marge effective ≥ 30 s avant déclenchement.

**Coût opérationnel** : zéro SMS en fonctionnement nominal (heartbeat OK). Pendant un épisode de heartbeat KO de durée D : `ceil(D / 2 min)` SMS. Pour le coût du forfait Sosh "SMS illimités" (~10€/mois) côté SIM gateway, marginal nul.

**Coexistence avec INV-060 (SMS d'alarme métier)** :
- INV-060 vise les notifications d'alarmes (`AlarmNotification` actives, déclenchées par escalade).
- INV-067 vise les pings de joignabilité (aucun lien avec une alarme métier).
- La colonne `SmsQueue.is_ping` (à ajouter) permet à la gateway de tracer les 2 flux. Le DLR d'un ping est stocké séparément (table `PingDeliveryReport`), pas dans `AlarmNotification.sms_sent`.
- Aucune interférence avec INV-062 (anti-doublon SMS d'alarme) parce que les pings ont `alarm_id = NULL` et un body unique (timestamp ISO différent à chaque envoi).

**Couverture à créer** (PR d'implémentation backend) :
- Unit `test_send_ping_when_pos1_heartbeat_lost_for_30s` : forcer pos1 offline 31s → SmsQueue contient 1 ping avec body `[ALARME-MURGAT-PING] ...`.
- Unit `test_no_ping_when_pos1_heartbeat_recent` : pos1 online → 0 ping inséré.
- Unit `test_no_ping_to_pos2` : seul pos1 reçoit des pings (pas pos2, pos3, etc.). Choix business : seul l'opérateur **de garde** est censé être réveillé localement.
- Unit `test_ping_interval_2min` : pos1 offline 5 min → exactement 2-3 pings (1 à 30s, 1 à 2m30s, 1 à 4m30s).
- Integration `test_pos1_reconnects_clears_last_ping_sent_at` : heartbeat 2xx → `last_ping_sent_at` reset, futur épisode redémarre à zéro.
- E2E `test_ping_visible_in_sms_queue_then_delivered` : nécessite Mailhog-like pour SMS ou la vraie gateway Mailhog.

**Dépendances** :
- `User.phone_number` doit être non-NULL pour pos 1 (sinon pas de destination — cf INV-061 réutilisé). Si NULL, log + skip + email direction technique 1× par épisode pour signaler le défaut de configuration.
- `gateway/modem_gateway.py` : aucun changement direct, le flux d'envoi standard convient. Mais le delivery report (DLR) doit être capté et POSTé à un nouvel endpoint `/internal/sms/delivery-report` (auth `X-Gateway-Key`) pour audit + futur usage. À spécifier dans une INV-068 séparée si besoin (hors scope INV-067 V1).
- INV-053 (email "personne en ligne") : non impacté, fonctionne en parallèle pour la chaîne entière. INV-067 est plus rapide à se déclencher (30s vs 15 min) et cible uniquement pos1, donc complémentaire.

### INV-066 [H] ✅ Toutes les références temporelles utilisent `clock_now()`, pas `datetime.utcnow()`
**Contexte** : le backend a une horloge injectable (`clock.py`) pour que les tests puissent simuler "15 minutes se sont écoulées" en appelant `POST /api/test/advance-clock?minutes=15` au lieu d'attendre réellement 15 minutes. `clock_now()` retourne `datetime.utcnow() + offset`. En prod, l'offset est à 0, donc `clock_now() == datetime.utcnow()`.

**Invariant** : toute écriture de timestamp (`notified_at`, `created_at`, `last_heartbeat`, `acknowledged_at`, `suspended_until`, etc.) doit utiliser `clock_now()`. Sinon, en test, une partie des timestamps est "temps simulé" et une autre est "temps réel" → incohérences.

**Fix** : PR3 (0716174). 3 call sites `notified_at` corrigés :
- `alarms.py::_add_notified_user` : ajout `notified_at=clock_now()`
- `test_api.py::send_test_alarm` (POST `/api/test/send-alarm`) : idem
- `test_api.py::trigger_escalation` (POST `/api/test/trigger-escalation`) : idem

**Couverture** : l'invariant est maintenant respecté dans tous les call sites. Pas de test unit dédié (la fonction pure ne voit que des snapshots avec dates déjà calculées), mais les tests E2E qui combinent advance_clock + création d'alarme valident implicitement.

**Écart latent connu (audit 2026-04-20)** : `send_alarm` dans `alarms.py:84-89` ne passe pas `created_at=clock_now()` explicitement ; l'objet `Alarm` récupère sa valeur via le default SQLAlchemy `datetime.utcnow` (`models.py:38`). Non problématique tant que les tests créent l'alarme **avant** tout `advance-clock`, mais un futur test qui ferait `advance-clock` puis `POST /alarms/send` verrait un `created_at` en « temps réel ». À forcer en `clock_now()` lors de l'implémentation d'INV-018 (ce sera le bon moment, puisqu'on touchera au modèle `Alarm`).

### INV-069 [H] ✅ Saisie des numéros de téléphone des opérateurs (onglet Utilisateurs, web admin)

**Contexte** : le backend **appelle déjà** (`CallQueue` → gateway SIM7600, `escalation.py::_enqueue_call`) et envoie des SMS (INV-060) à tout user disposant d'un `phone_number`. Le champ `User.phone_number` existe (`models.py:17`), est exposé dans `UserResponse` (`schemas.py:27`) et éditable via `PATCH /api/users/{id}` (`users.py:165`). **Mais aucun écran ne permet de le saisir** → le canal SMS/appel de l'escalade est inexploitable en pratique, faute de destination.

**Invariant** : l'admin doit pouvoir saisir/éditer le numéro de chaque opérateur depuis le web, pour alimenter les canaux SMS/appel de l'escalade (INV-060/061/063).

Sous-règles :
1. **Tableau Utilisateurs** : chaque ligne expose un champ `input.user-phone` pré-rempli avec le `phone_number` courant + un bouton `.save-phone-btn`. Le clic envoie `PATCH /api/users/{id}` body `{"phone_number": <valeur>}` puis recharge.
2. **Création** : le formulaire « Ajouter un utilisateur » a un champ `#newUserPhone` ; `addUser()` inclut `phone_number` dans le POST `/api/auth/register`. Nécessite l'ajout de `phone_number: Optional[str]` à `UserCreate` (`schemas.py`) et sa persistance dans `register()` (`users.py`).
3. **Validation** : avant envoi, la valeur est débarrassée de ses espaces. Une valeur non vide doit matcher `^\+?[0-9]{6,15}$` (format composable par le modem SIM7600). Si invalide → message d'erreur visible (`.phone-error`) et **aucune requête** envoyée. Une valeur **vide est autorisée** (efface le numéro → NULL, cf INV-061 « pas de SMS/appel si NULL »).
4. **Signalement** (onglet Escalade) : tout membre de la chaîne dont `phone_number` est vide affiche un badge `.no-phone-badge` (« pas de tél »), pour rendre visible le risque d'être silencieusement zappé pour SMS/appel (INV-061).

**Pourquoi** : sans saisie, les numéros restent NULL et l'escalade ne contacte jamais personne par SMS/appel — le canal de secours (quand le push FCM ne réveille pas l'opérateur) est mort. Le badge (4) supprime l'angle mort « membre de la chaîne injoignable, sans alerte ».

**Hors scope** : édition du numéro côté app Android (login par nom only, pas de profil éditable) ; normalisation internationale avancée (libphonenumber) ; vérification de joignabilité réelle (DLR, cf note INV-067/INV-068).

**Statut** : implémenté + 5 tests GREEN (session Claude 2026-06-15) — PR #165. Frontend `index.html` (`loadUsers`/`savePhone`/`addUser`/`noPhoneBadge`) + backend `UserCreate.phone_number`/`register`.

**Couverture** (E2E Playwright — `tests/test_frontend.py::TestUsersTab`) :
- `test_users_table_has_phone_input_per_user` — chaque ligne a un `input.user-phone`.
- `test_save_phone_sends_patch_to_user_endpoint` — édition + save → `PATCH /api/users/{id}` avec `phone_number`.
- `test_add_user_with_phone_appears_in_table` — création avec numéro → numéro persisté et réaffiché (valide aussi `UserCreate`/`register`).
- `test_invalid_phone_shows_error_and_no_request` — numéro invalide → `.phone-error` visible, aucun PATCH.
- `test_escalation_chain_flags_member_without_phone` — membre de chaîne sans numéro → badge `.no-phone-badge`.

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

### INV-074 [C] ✅ Refresh token produit un nouveau access token (mode legacy Bearer)
POST /auth/refresh avec Bearer valide → nouveau access token différent.
- **Pourquoi** : un refresh qui renverrait le token reçu en input ne sert à rien (pas de rotation TTL).
- **Implémentation** : `create_access_token` (auth.py:28-37) inclut `"jti": str(uuid.uuid4())` dans le payload → chaque token a un UUID unique → tokens trivialement distincts à chaque appel.
- **Statut 2026-06-15** : mode conservé pour rétro-compat. Le mode canonique est INV-079 ci-dessous (refresh dans le body, sans Bearer requis). L'endpoint accepte les deux ; le body INV-079 gagne s'il est présent.
- **Fix** : PR #92 (bot IA, 2026-05-13). Aucune modif prod ; verrouillage en régression via 2 tests.
- **Couverture** : `tests/integration/test_auth_refresh.py` (tier 2) + `tests/test_e2e.py::TestRefreshTokenInv079::test_refresh_legacy_mode_bearer_still_works` (régression tier 3).

### INV-079 [C] ⚠️ Refresh token persistant DB-backed, jamais expiré sauf révocation (2026-06-15)
Pattern Gmail/OAuth2 : `/auth/login` renvoie un `refresh_token` (UUID4 opaque) **en plus** de l'access token JWT. Le SHA-256 du refresh est persisté dans une nouvelle table `refresh_tokens(id, user_id, token_hash UNIQUE, created_at, last_used_at, revoked)` et **n'expire jamais** sauf si `revoked=TRUE`. Le client utilise `POST /auth/refresh` **avec le refresh dans le body** (sans Authorization header — l'access JWT peut être expiré) et obtient un nouvel access valide 24h.
- **Pourquoi** : avant INV-079, le seul moyen de renouveler l'access était d'envoyer le Bearer JWT lui-même (INV-074). Si l'app restait éteinte > 24h, le JWT expirait, le refresh échouait, l'utilisateur devait retaper son mdp. Pour les téléphones d'astreinte qui peuvent rester en veille plusieurs jours, c'était une friction inacceptable. Pattern Gmail : refresh token éternel côté serveur, access court côté client.
- **Sécurité** : le refresh est un UUID4 opaque (pas un JWT), validé uniquement par lookup DB sur son **SHA-256** (le clair n'est JAMAIS persisté — une lecture de la DB ne donne pas de tokens réutilisables). Hash déterministe sans sel : inutile, le UUID4 a 122 bits d'entropie (pas de brute-force). Cela permet la **révocation** côté serveur (impossible avec JWT stateless). Si un tél est volé, admin peut faire `UPDATE refresh_tokens SET revoked=TRUE WHERE user_id=...` → tous les refresh du user sont rejetés à la prochaine utilisation, sans tourner le `SECRET_KEY` JWT (qui déconnecterait tout le monde).
- **Pas de rotation V1** : un refresh donné peut être réutilisé indéfiniment (cf test `test_refresh_token_survives_multiple_uses`). La rotation à l'usage est une éventuelle V2.
- **Implémentation** :
  - Modèle `RefreshToken` (`backend/app/models.py`) — colonne `token_hash` (SHA-256), pas le clair
  - Migration `_migrate_refresh_tokens` dans `run_migrations()` (idempotent, SQLite + PostgreSQL). La contrainte UNIQUE sur `token_hash` crée l'index ; seul `user_id` a un `CREATE INDEX` explicite.
  - Helpers `_hash_refresh_token`, `create_refresh_token`, `validate_refresh_token`, `revoke_refresh_tokens_for_user` dans `auth.py` (le dernier pas encore branché à un endpoint, cf limites)
  - `get_current_user_optional` (`auth.py`) : version tolérante de `get_current_user` qui retourne `None` sur Bearer absent/invalide au lieu de raise 401 — permet à `/auth/refresh` de basculer sur le mode body.
  - `/api/auth/login` : ajout de `refresh_token` dans la `TokenResponse`
  - `/api/auth/refresh` : accepte `RefreshRequest(refresh_token: str)` body OU Bearer (legacy INV-074). Body prioritaire si les deux sont fournis.
- **Couverture** (`tests/test_e2e.py::TestRefreshTokenInv079`, tier 3) — 7 tests :
  - `test_login_returns_refresh_token` (format UUID4 dans la response /login)
  - `test_refresh_via_body_works_without_bearer` (mode canonique)
  - `test_refresh_via_body_with_invalid_token_returns_401`
  - `test_refresh_legacy_mode_bearer_still_works` (rétro-compat INV-074)
  - `test_refresh_neither_body_nor_bearer_returns_401`
  - `test_refresh_with_invalid_bearer_and_valid_body_returns_new_access` (cas réel : Bearer expiré + refresh valide → mode body prime)
  - `test_refresh_token_survives_multiple_uses` (pas de rotation V1)
- **Côté client (Android)** : cf INV-ANDROID-505/506 mis à jour. Refresh stocké dans `SharedPreferences("alarm_prefs").refresh_token` au login. `tryRefreshToken()` lit ce refresh et l'envoie en body via la nouvelle signature `ApiService.refreshToken(RefreshRequest)`.
- **Limites identifiées (issues follow-up à ouvrir)** :
  - [H] : pas de `/auth/logout` qui révoque le refresh côté serveur. Aujourd'hui le logout local clear les prefs uniquement — le refresh reste valide en DB. Un user qui logout+relogin a 2 refresh tokens valides en DB.
  - [H] : pas de mécanisme de révocation depuis l'UI admin web (`UPDATE` SQL à la main).
  - [M] : pas de purge périodique des refresh tokens `last_used_at < 90 jours` (croissance illimitée de la table).

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

### INV-153 [H] ✅ Un téléphone n'est inscrit qu'une fois, pour un seul utilisateur (2026-10-22)
Une installation de l'app est identifiée par son jeton FCM, plus précisément par l'identifiant d'installation Firebase (la partie du jeton avant `:` ; jeton entier s'il n'y en a pas).
- **Inscription** (`POST /api/devices/fcm-token`) : toute autre inscription de la même installation est supprimée, **quel que soit l'utilisateur** (un téléphone n'est connecté qu'à un compte à la fois).
- **Déconnexion** (`DELETE /api/devices/fcm-token`) : toutes les inscriptions **de cet utilisateur** pour cette installation sont supprimées, pas seulement celle du `device_id` envoyé. Ses autres téléphones restent inscrits (INV-078).
- **Pourquoi** : prod 2026-10-22, timo déconnecté de tous ses téléphones restait « En attente » (INV-143) : 2 inscriptions fantômes (13 et 14 oct., même jeton, `device_id` différents) laissées par des déconnexions dont l'appel serveur n'était pas parti (INV-ANDROID-503) ; chaque connexion recrée un `device_id`. Le même téléphone était entre-temps connecté au compte `test`.
- **Couverture** : Integration `test_fcm_one_registration_per_install_inv153.py`.

### INV-078 [M] ✅ Logout supprime le token FCM
POST /devices/fcm-token DELETE → retire de la base, plus de push reçu.
- **Fix** : PR #95 (bot IA, 2026-05-13). Aucune modif prod (`delete_fcm_token` dans `devices.py:126-139` exécute déjà `db.query(DeviceToken).filter(...).delete() + commit`).
- **Couverture** : `tests/integration/test_fcm_logout.py` (tier 2, 2 tests) :
  - `test_delete_fcm_token_removes_db_row_inv078` (cœur : présence avant / absence après, vérification DB directe indépendante du chemin API)
  - `test_delete_fcm_token_only_removes_target_device_inv078` (défense en profondeur : suppression scopée au `device_id`, pas de wipe massif)
- **Note méthodo** : le 1er run bot avait abandonné (P5 strict sur cas ambigu — le body issue ne disait pas explicitement "verrouille en régression"). Issue #72 reformulée + PR #94 (`.github/ai-bot/prompt.md`) ajoute la section "Cas special : code deja conforme (regression lock)" pour éviter cette classe de faux-abandons sur le reste du backlog.

### INV-151 [H] ✅ Rôle « test » : le téléphone d'un compte test ne sonne pas pour les alarmes (2026-10-22)
Un utilisateur porte un booléen `is_test` (défaut `false`), cumulable avec `is_admin`. Comme `is_admin`, il se choisit **à la création** et ne se modifie pas ensuite (recréer le compte pour changer).
- **Création (web)** : le formulaire « Ajouter un utilisateur » a un choix `#newUserTest` (Non/Oui) ; `addUser()` envoie `is_test` dans `POST /api/auth/register`.
- **API** : `is_test` est renvoyé dans `UserResponse` : `GET /api/users/`, `GET /api/auth/me` et le `user` de `POST /api/auth/login` (c'est là que l'app le lit, cf INV-ANDROID-008). Un compte créé avant cet invariant vaut `false`.
- **Onglet Utilisateurs** : colonne « Test », cellule `td.user-test` à « Oui » ou « Non ».
- **Onglet Escalade** : tout membre de la chaîne avec `is_test` affiche un badge `.test-badge` « test, ne sonne pas ». Les autres membres n'en ont pas.
- **Numéro** : livré d'abord sous « INV-148 » par #253, numéro déjà pris le même jour par la gateway (#252) → renuméroté INV-151.
- **Livré en deux temps** (cf `docs/CD_DESIGN.md`, migration de schéma) : (1) PR #253, colonne `users.is_test BOOLEAN NOT NULL DEFAULT FALSE` lue par aucun code ; (2) une fois la colonne créée par le leader en prod, le code qui la lit (API, web, app).
- **Rien d'autre ne change côté serveur** : un compte test reçoit les alarmes, l'escalade le traite comme les autres (15 min puis le suivant, INV-011), SMS et appels inchangés (pas de numéro → pas d'appel, INV-061).
- **Pourquoi** : demande du propriétaire du 2026-10-22 : un compte qui sert aux tests (alarmes envoyées en prod, essais de l'app) sans faire sonner le téléphone. Le badge rend visible qu'une vraie alarme arrivée sur ce compte attend 15 min en silence avant de passer au suivant.
- **Couverture** : Integration `test_user_is_test_inv151.py` (rôle posé à la création et renvoyé au login, défaut `false`, migration d'une base existante) ; Playwright `test_frontend.py::TestUsersTab::test_add_test_user_shows_test_column`, `::test_escalation_chain_flags_test_member`.

### INV-149 [C] ✅ Seul un admin peut créer un compte (2026-10-22)
`POST /api/auth/register` exige un jeton d'admin : sans jeton → refus (`401` ou `403`), jeton d'un non-admin → `403`. Dans les deux cas **aucun compte n'est créé**. L'admin crée les comptes depuis l'onglet Utilisateurs (déjà authentifié).
- **Pourquoi** : décision du propriétaire du 2026-10-22. La route était publique : quiconque joignait l'API pouvait se créer un compte, y compris admin (`is_admin` dans le body), puis lire et modifier la chaîne d'escalade.
- **Couverture** : Integration `test_register_admin_only_inv149.py` (sans jeton, non-admin).

### INV-162 [C] ✅ Seul un admin peut résoudre une alarme (2026-10-29)
`POST /api/alarms/{id}/resolve` exige un jeton d'admin : jeton d'un non-admin → `403` et l'alarme **reste dans son état** (pas de `resolved`) ; sans jeton → refus (`401` ou `403`). Un admin résout comme avant : audit `alarm_resolved` et push `alarm_resolved` à chaque notifié (INV-147).
- **Non concerné** : la résolution automatique par la gateway (`/internal/alarms/report-state` à `closed`, INV-120), authentifiée par `X-Gateway-Key`, et l'acquittement (INV-031), toujours ouvert aux notifiés.
- **Pourquoi** : décision du propriétaire du 2026-10-29 : « résoudre une alarme : admins uniquement ». Tout utilisateur connecté pouvait résoudre n'importe quelle alarme, donc arrêter l'escalade et la sonnerie de tous.
- **Couverture** : Integration `test_version_permissions_inv162_164.py` : `test_non_admin_cannot_resolve` (user1 notifié → 403, alarme toujours `active` ; RED avant le correctif : 200), `test_admin_resolves`.

### INV-163 [H] ✅ Seul un admin peut lire la configuration système (2026-10-29)
`GET /api/config/system` (toutes les clés `SystemConfig` : `alert_email`, états modem, réglages d'escalade et de watchdog…) exige un jeton d'admin : sans jeton → refus (`401` ou `403`), jeton d'un non-admin → `403`, aucune valeur renvoyée. L'écriture (`POST /api/config/system`) l'exigeait déjà (INV-077). La chaîne d'escalade publique (`GET /api/config/escalation`) et l'état du modem (`GET /api/system/modem-status`) ne changent pas.
- **Pourquoi** : décision du propriétaire du 2026-10-29. La route était publique : quiconque joignait l'API lisait l'adresse d'alerte et les réglages internes.
- **Couverture** : Integration `test_version_permissions_inv162_164.py` : `test_system_config_refused_without_token`, `test_system_config_refused_for_non_admin` (RED avant le correctif : 200 avec les valeurs), `test_system_config_for_admin`.

---

## 8. Chaîne d'escalade et config

### INV-080 [C] ✅ Chaîne vide + alarme envoyée → email
POST /alarms/send avec chaîne vide → email direction technique, alarme persiste avec fallback user.
- **Pourquoi** : ne pas perdre l'événement.
- **Fix/Extraction** : PR5 (cc35b7d). Logique dans `logic/alarm_creation.py`.
- **Couverture** :
  - Unit : `test_alarm_creation.py::TestInv080ChainEmpty` (3 tests : fallback 1er user, pas d'user, override explicite + email)
  - E2E : `TestEmptyEscalationChainAlert::test_alarm_with_empty_chain_sends_email` + `TestEmailViaMailhog::test_empty_chain_sends_real_email`

### INV-081 [M] ⚠️ /config/escalation/bulk remplace la chaîne, répond sans attendre les push (amendé 2026-11-13)
`POST /api/config/escalation/bulk` → DELETE + INSERT de la chaîne (une transaction, INV-082), puis un push FCM `escalation_update` à chaque utilisateur de la nouvelle chaîne : « Vous etes de garde » en position 1, « Chaine d'escalade modifiee » aux suivants (`escalation_position`, `is_oncall`).
- **La réponse n'attend pas les push** : `200` avec la nouvelle chaîne dès qu'elle est enregistrée. Les push partent ensuite, hors de la requête, avec leur propre session DB. Un envoi lent ou bloqué (Google injoignable) ne retarde pas la réponse. Un échec des push est journalisé, jamais perdu en silence, et ne touche jamais la chaîne enregistrée.
- **Tests** : `/api/test/last-fcm`, `/api/test/reset-fcm` et `/api/test/reset` attendent la fin des push en cours avant de lire ou de vider la liste des push.
- **Pourquoi** : prod 2026-11-13 (node3) : enregistrer la chaîne depuis l'app affichait « Serveur injoignable » alors que la chaîne était enregistrée. Le handler envoyait les push avant de répondre : oauth2.googleapis.com injoignable ~15 s (jeton OAuth), puis ~10 envois à des jetons morts (404 NotRegistered). L'app (OkHttp, lecture 5 s) avait coupé la connexion bien avant. `POST /api/config/escalation-delay`, sans push, répondait en 5 ms.
- **Couverture** : E2E `TestEscalationChainBulk::test_save_escalation_chain_replaces_all` ; Integration `test_escalation_chain_push_inv081.py` (envoi FCM bloqué 3 s par jeton : réponse < 1 s, chaîne enregistrée, push envoyés ensuite aux mêmes utilisateurs avec les mêmes titres ; push en échec : `200`, chaîne gardée, échec dans les logs).

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
- **Limite (2026-10-30)** : ce contrôle ne tourne que sur le leader. Quand il n'y a **plus aucun leader** (cas même de la perte du quorum etcd : le leader Patroni se rétrograde), cet email ne part pas. Couvert par **INV-170** (email depuis chaque nœud, sans leader).

---

## 9. Cluster et haute disponibilité

### INV-090 [C] ⚠️ Exactement 1 primary
À tout instant, `sum(role == 'primary' for noeud in cluster) == 1`.
- **Voteurs etcd (précisé le 2026-11-05, décision du propriétaire)** : **aujourd'hui 3 voteurs**, node1, node2 (onsite-1/2, « Legacy (Izeron) », bientôt retirés) et node3 (OVH), quorum 2 ; node4 à node7 non votants. Même liste partout : `ETCD_HOSTS` / `ETCD_INITIAL_CLUSTER` des `.env.prod.nodeN`, `etcd_voters` (INV-175), `VOTERS=(1 2 3)` de l'orchestrateur (INV-167, passé au canary par `--voters`, INV-187), `start-prod-node.sh` (etcd sur 1 à 3 seulement).
- **Bascule le jour du déménagement seulement** : état final **3 voteurs node3, node6, node7** (alarm-3/4 branchés à Izeron), node1/node2 retirés d'etcd et plus déployés. Passage par 4 puis 5 membres (`etcdctl member add` de node6 puis node7), puis `member remove` de node1 et node2 ; runbook et configuration finale dans une PR brouillon dédiée (« NE PAS FUSIONNER AVANT LE JOUR J »).
- **Pourquoi pas avant** : tant qu'alarm-3/4 sont physiquement à Beaufort, tout le trafic entre sites passe par le relais NODE3. Avec 5 voteurs (node1, node2, node3, node6, node7), OVH en panne laisserait Izeron avec 2 voteurs et Beaufort avec 2 : aucun côté n'a les 3 du quorum, plus de leader ; avec les 3 voteurs d'aujourd'hui, Izeron le garde (2 sur 3).

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

### INV-125 [H] ✅ Bring-up refusé si etcd/PG/Patroni seraient exposés hors du mesh
Le bring-up d'un nœud doit **échouer avant** `docker compose up` si la valeur de
`ADVERTISE_HOST` ferait publier etcd (`2379`/`2380`), PostgreSQL (`5432`) et Patroni
REST (`8008`) sur toutes les interfaces **alors que la machine porte au moins une
adresse IPv4 publiquement routable**.

Trois cas refusés : `ADVERTISE_HOST` absent/vide/wildcard (`0.0.0.0`, `::`, `*`),
égal à une adresse publiquement routable, ou non littéral (nom d'hôte — invérifiable
sans résolution DNS, donc *fail closed*).

Un cas toujours autorisé : **aucune adresse publiquement routable sur la machine**
(CI/dev mono-host, onsite derrière NAT). Le garde-fou ne doit jamais refuser ce
cas, sinon il casse la CI, qui lance 3 nœuds sur un host sans `ADVERTISE_HOST`.

- **Pourquoi** : les règles iptables de Docker court-circuitent UFW — un port publié
  sur `0.0.0.0` reste joignable depuis Internet alors que `ufw status` l'affiche fermé.
  Les trois surfaces sont **non authentifiées** : l'API etcd v3 n'a aucune auth (prise
  de contrôle du DCS), PostgreSQL utilise des identifiants présents dans le repo, et
  `/switchover`, `/failover`, `/restart` de Patroni ne demandent rien. Exposition
  constatée sur le nœud cloud jusqu'au **2026-06-29**. Depuis le correctif de bind
  mesh-only, la seule chose qui ferme ces ports est la présence d'`ADVERTISE_HOST`
  dans un fichier d'env **hors repo** : un `.env` qui l'oublie retombe sur le défaut
  et republie tout **en silence**, sans rien dans les logs.
- **Portée IPv4 seulement** : le défaut `0.0.0.0` du compose ne publie que sur IPv4,
  donc une IPv6 publique sur la machine n'est pas une exposition et ne doit pas
  bloquer le bring-up.
- **Couverture** : `tests/unit/test_bind_exposure.py` (logique pure
  `backend/app/logic/bind_exposure.py` + code de sortie du CLI). Le CLI
  `scripts/check-bind-exposure.py` est appelé par `scripts/start-prod-node.sh`.
- **Convention mutation** : les littéraux de `message=` portent `# pragma: no mutate`.
  mutmut emballe un littéral entier (`"texte"` → `"XXtexteXX"`), donc une assertion
  par sous-chaîne ne peut pas tuer ces mutants — il faudrait comparer la prose au
  caractère, ce qui rendrait les tests cassants à chaque reformulation. Ce qui est
  testé est le contenu **opératoire** du message : l'adresse qui expose la machine.
  Même traitement que `frozen=True` sur les dataclasses de verdict (cf INV-085).

### INV-152 [C] ✅ Un nœud qui devient leader applique les migrations de schéma en attente (2026-10-22)
Les migrations (`create_all` + `run_migrations`, idempotentes) sont jouées **au démarrage** du backend **et à chaque fois que le nœud devient leader** Patroni. Si elles échouent (base pas encore inscriptible, erreur), le leader réessaie à chaque tour de l'élection (5 s) jusqu'à réussir.
- **Pourquoi** : constaté en prod le 2026-10-22 (#253, colonne `users.is_test`). Le canary redémarre les backends **et** Patroni : le leader change pendant le déploiement (node2 → node3), chaque backend démarre en replica (`Schema init skip — replica read-only`) et aucun ne rejoue les migrations en devenant leader → colonne jamais créée sur aucun nœud. Le code qui l'aurait lue (#254) aurait renvoyé des 500 sur toute requête `users` (login, heartbeats) **sur les 3 nœuds**. Le correctif #144 supposait à tort qu'un nœud promu plus tard ferait l'init.
- **Couverture** : Integration `test_schema_init_on_promotion_inv152.py` : 1re promotion (`test_node_promoted_after_boot_applies_pending_migrations`), retry jusqu'au succès après False puis exception puis plus rappelé (`test_leader_retries_migrations_each_tour_until_success`), re-promotion leader → replica → leader rejoue les migrations (`test_node_promoted_again_reapplies_migrations`) (#256).

### INV-161 [M] ✅ Cluster : chaque nœud affiche sa machine et son lieu (Izeron, Beaufort, OVH) (2026-10-27, amendé le 2026-11-05 et le 2026-11-06)
`GET /api/cluster` ajoute à chaque membre `location` (lieu), `machine` (nom de la machine) et `retiring` (booléen : machine « bientôt obsolète »), et renvoie `local_location` et `local_machine` pour le nœud qui répond. Trois clés SystemConfig, avec ces défauts y compris sur une base déjà en service (aucun seed ne les écrit) :
- `node_locations` (`nom:lieu` séparés par des virgules) : `node1:izeron,node2:izeron,node3:ovh,node4:beaufort,node5:beaufort,node6:izeron,node7:izeron`. Un nœud absent de la liste a `location = null`.
- `node_machines` (`nom:machine`) : `node1:onsite-1,node2:onsite-2,node3:OVH,node4:alarm-1,node5:alarm-2,node6:alarm-3,node7:alarm-4`. Un nœud absent de la liste a pour `machine` son nom Patroni (jamais `null`).
- `retiring_nodes` (noms séparés par des virgules) : `node1,node2`. `retiring = true` pour ces nœuds, `false` sinon.
- Une clé enregistrée en base **remplace entièrement** son défaut (aucune fusion) : si `node_locations` a été posée à la main avant le 2026-11-05, il faut y ajouter `node6:izeron,node7:izeron`.
- **503** (Patroni local injoignable) : `local_node` seul, comme avant (pas de `local_machine`) : aucune lecture de la base, dont la connexion passe par ce Patroni.
- **Web** (onglet Cluster) : colonne « Nœud » = `machine · nom` (« alarm-3 · node6 » ; le nom seul si la machine est inconnue) avec un badge ambre « bientôt obsolète » si `retiring` ; le lieu est le titre de la carte du groupe (cf. « Cartes par groupe ») ; carte « Ce nœud » = `machine · nom (Lieu)` (« Izeron », « Beaufort », « OVH » ; sans parenthèses si inconnu). Même libellé `machine · nom` dans l'onglet Tests : tableau des nœuds de « Résilience » (avec le badge, INV-175) et options de cible de « Panne contrôlée » (« — bientôt obsolète » en fin d'option, INV-179). Textes échappés (`escHtml`), `data-label` gardés pour le mobile.
- **Changement de lieu ou de machine** : on modifie `node_locations` / `node_machines` / `retiring_nodes`, sans redéploiement. Exemple : quand alarm-3/4 (node6/7) seront à Izeron, rien à changer ; quand onsite-1/2 seront retirés, les ôter des trois clés.
- **node6 / node7 (décision du propriétaire, 2026-11-05)** : alarm-3 (node6, WireGuard 10.99.0.6) et alarm-4 (node7, 10.99.0.7), futurs nœuds d'Izeron, rejoignent le cluster de prod comme **non votants** (Patroni + backend, sans etcd, comme node4/node5 ; docs/REPLACE_ONSITE_NODE.md). Ils sont étiquetés `izeron` dès maintenant alors qu'ils sont **physiquement à Beaufort jusqu'au déménagement**. **Limite assumée** : le panneau Résilience (INV-175) et la panne contrôlée (`site:izeron`, INV-176/177) les comptent à Izeron ; une vraie panne de Beaufort les emporterait aussi, ce que le panneau ne montre pas. onsite-1/onsite-2 (node1/node2) restent voteurs etcd et en service jusqu'au déménagement, affichés « bientôt obsolète ».
- **Pourquoi** : cluster de prod à 5 nœuds sur 3 lieux depuis le 2026-10-27 (NODE3 OVH, onsite-1/2 Izeron, alarm-1/2 « Beaufort »). Le propriétaire veut voir d'un coup d'œil où se trouve chaque nœud. 2026-11-05 : 7 nœuds ; « node6 » ne dit pas de quelle machine il s'agit, ni lesquelles vont partir.
- **Groupes d'affichage (amendé le 2026-11-05, décision du propriétaire)** : un seul cluster (une base, une chaîne d'astreinte), montré en **groupes** : `group` (texte) ajouté à chaque membre de `GET /api/cluster` et à chaque nœud de `GET /api/resilience` `live.nodes`, calculé côté serveur (logique pure `logic/resilience.py::group_of`) : nœud `retiring` → « Legacy (Izeron) » (quel que soit son lieu : règle du propriétaire), sinon son lieu (« Izeron » pour alarm-3/4, « Beaufort » pour alarm-1/2, « OVH » pour node3) ; lieu inconnu → « Lieu inconnu ». **Libellés exacts** (casse et orthographe) : « Izeron », « Beaufort », « OVH », « Legacy (Izeron) » — contrat avec l'app Android (PR #297), qui retombe sur la même règle si `group` manque. **Ordre des groupes** (web et app) : Izeron, Beaufort, OVH, puis tout autre libellé (ordre alphabétique), « Legacy (Izeron) » **en dernier**. **Web** : onglet Cluster en cartes (ci-dessous) ; tableau des nœuds de « Résilience » (onglet Tests) groupé, une ligne d'en-tête par groupe (`tr.group-row`, texte du groupe, sans `data-label` ; « Legacy (Izeron) » séparé visuellement : `tr.group-row.legacy`, bordure ambre), nœuds triés par nom dans leur groupe (`tr[data-node]`). Cible de « Panne contrôlée » : un `<optgroup label="<groupe>">` par groupe (même ordre), puis les sites entiers dans `<optgroup label="Sites entiers">`.
- **Cartes par groupe (onglet Cluster, amendé le 2026-11-06, demande du propriétaire : « mieux différencier les machines d'Izeron et de Beaufort »)** : le bandeau quorum, les totaux et « Ce nœud » ne changent pas. Les membres sont montrés en **une carte par groupe** (`#clusterMembers .site-card[data-group="<groupe>"]`), dans l'ordre des groupes ci-dessus (Izeron, Beaufort, OVH, autres libellés, « Legacy (Izeron) » en dernier), chacune avec son identité de couleur, une classe CSS définie une seule fois : `site-izeron` (bleu), `site-beaufort` (vert d'eau), `site-ovh` (violet), `site-legacy` (ambre, bordure **en pointillés**), `site-other` (gris) pour tout autre libellé ; bordure gauche et pastille de titre (`.site-chip` = texte du groupe) de cette couleur.
  - **En-tête** : point d'état `.status-dot` **`online` (vert)** si **chaque** membre du groupe est soit le leader `running`, soit `streaming` sur la timeline du leader ; **`offline` (rouge)** sinon (ex. replica `running` sur une ancienne timeline, membre arrêté, aucun leader), avec un libellé accessible (`aria-label`) ; résumé `.site-sum` = « N machines · M en flux » (« 1 machine » au singulier ; M = membres `streaming`), suivi de « · leader ici » **seulement** si le leader est dans ce groupe. Carte « Legacy (Izeron) » : note `.site-note` « bientôt obsolète — à débrancher le jour du déménagement ».
  - **Lignes** : une par machine (`tr[data-node]`, triées par nom) : Nœud (`machine · nom` + badge), Role (badge), Etat, Timeline, Lag, Host ; la ligne du leader est mise en évidence (`tr.leader`). Plus de colonnes Lieu ni Port (le lieu est le titre de la carte, le port est le même partout). Textes échappés (`escHtml`).
  - **Version (2026-11-06, demande du propriétaire : « afficher les versions courantes de chaque nœud sur la page cluster »)** : colonne Version (`td[data-label="Version"]`) = version que le backend du nœud annonce sur `/health` (INV-164), lue dans `GET /api/resilience` (`live.nodes[].version`, INV-175 ; l'onglet Cluster est réservé aux admins). Les cartes s'affichent **sans attendre** cette lecture (« … » en attendant) ; nœud dont le backend ne répond pas (`reachable` faux) → « injoignable » ; version absente ou `/api/resilience` en erreur → « ? ». `/api/cluster` (public, appelé par le pied de page, l'app et le runner de panne avec 3 s de délai) ne sonde pas les nœuds.
  - **Mobile** : lignes en cartes empilées (`data-label`) ; à 375 px, ni la page ni une carte ne défile horizontalement.
- **Couverture** : Integration `test_cluster_location_inv161.py` (défauts des 7 nœuds : lieu, machine, `retiring`, `local_machine`, `group` ; clés lues dans SystemConfig ; nœud inconnu → nom Patroni, `retiring` faux, groupe « Lieu inconnu » ; `retiring_nodes` change le groupe ; libellés exacts épinglés) ; Unit `test_resilience.py` (`group_of`) ; Playwright `test_frontend.py::TestClusterTab` (hors CI, `/api/cluster` simulé avec les 7 nœuds de prod : cartes dans l'ordre, chaque machine dans sa carte, classe de couleur par carte, résumé et « leader ici », ligne du leader, note Legacy, point rouge si un membre est `running` sur une ancienne timeline, 375 px sans défilement horizontal ; libellé inconnu → `site-other` ; Version par nœud, « injoignable », « ? » si `/api/resilience` échoue, cartes affichées avant sa réponse), `TestResiliencePanel`, `TestChaosConfirm` (`optgroup`).

### INV-167 [C] ✅ CD : un nœud injoignable ne bloque pas le déploiement des autres sites (2026-10-29)
`alarm-cd-orchestrate` (NODE3, ordre `3 7 6 5 4 2 1` depuis le 2026-11-05 : NODE3 cloud d'abord comme avant, puis les non votants du plus récent au plus ancien, onsite-2 puis onsite-1 en dernier) déploie chaque nœud indépendamment :
- **Nœuds** : node1 à node7 (IP WireGuard : node1 `10.99.0.2`, node2 `10.99.0.3`, node3 `10.99.0.1`, nodeN `10.99.0.N` pour N = 4 à 7). Votants etcd : node1, node2, node3 ; non votants : node4 à node7, que `start-prod-node.sh` (appelé par le canary) lance sans etcd (`up --no-deps patroni backend`). `alarm-cd-canary.sh` et `alarm-cd-rollback.sh` acceptent `--node 1` à `7` et cherchent le leader sur les 7 IP.
- **État par nœud** `/var/lib/alarm/cd/node<N>.digest` = digest déployé avec succès sur node N (écrit après son canary OK). Un nœud déjà au digest courant est sauté ; les 7 à jour → rien à faire. Tant que node6/node7 ne sont pas installés, chaque passage les trouve injoignables : `partial`, sans cooldown, les autres nœuds sont déployés.
- **Nœud injoignable** (SSH) → `WARN node<N> (site) injoignable : reporté au prochain passage`, la chaîne **continue** avec les nœuds suivants.
- **Quorum etcd** (INV-090) : un votant (node1, node2, node3) n'est redémarré que si **les 3 votants** répondent au préflight. Sinon aucun votant ce passage, mais les non votants (node4 à node7) joignables sont déployés. Depuis le 2026-11-05, les votants sont le tableau `VOTERS=(1 2 3)` du script (au lieu de « `NODE <= 3` ») : le jour du déménagement, seuls `VOTERS` et `NODES_ORDER` changent ; comportement inchangé (mêmes tests).
- **Canary en échec** (health KO → rollback) → la chaîne s'arrête, `failure` + cooldown 1 h : une image cassée ne se propage pas.
- **Nœud pas prêt** (amendé le 2026-11-02) : le canary sort en `3` quand ses **prérequis** échouent, avant de toucher au nœud (SSH coupé entre le préflight et l'étape, ou digest distant `alarm-backend:stable` illisible depuis la cible : registre ou DNS injoignable). Ce n'est pas le signe d'une image cassée : `WARN node<N> (site) pas prêt : reporté au prochain passage`, la chaîne **continue**, passage `partial` sans cooldown. Un **votant** reporté ainsi reporte aussi les votants suivants du passage (il est peut-être tombé : en redémarrer un autre perdrait le quorum, INV-090). Depuis le 2026-11-06, même sortie `3` quand la cible est le **leader Patroni** et que la bascule planifiée préalable n'a pas abouti (INV-187) : la cible n'est pas redémarrée.
- `orchestrate.state` (`<digest>\t<outcome>\t<ts>`) : `success` (7 nœuds à jour), `partial` (nœuds en attente) ou `failure`. Un passage `partial` **ne déclenche pas de cooldown** : le tick suivant reprend aussitôt les seuls nœuds en attente. Un nœud qui revient reçoit le digest courant, même inchangé depuis le dernier passage réussi.
- **Version promue pendant un passage** (amendé le 2026-11-09, demande du propriétaire) : l'orchestrateur fixe le digest au début du passage (cache `:stable` de NODE3), mais chaque canary déploie l'image `:stable` la plus récente présente sur sa cible. En fin de passage (`success` ou `partial`, pas après un `failure`), si le digest en cache sur NODE3 **a changé**, `nouvelle image promue pendant le passage (<digest>) : nouveau passage tout de suite` et un nouveau passage complet démarre **aussitôt**, sans attendre le tick suivant ; les nœuds déjà sur la nouvelle image en sortent en quelques secondes (canary « Pas de mise a jour », ni bascule ni soak) et reçoivent le bon `node<N>.digest`. **Au plus 3 passages par tick** (garde-fou) : au-delà, `… 3 passages faits, repris au prochain tick`. Pourquoi : le 2026-11-09, 1.9.3 a été promue à 08:25Z pendant un passage commencé à 08:20Z sur la version précédente : node3 (OVH, première adresse de l'app), node7 et node6 sont restés un passage de plus sur l'ancienne version, et l'app ne proposait pas la 1.9.3.
- **Tick 2 min** (amendé le 2026-11-09, décision du propriétaire) : `alarm-cd-orchestrate.timer` `OnUnitActiveSec=2min` (15 min avant), gigue `RandomizedDelaySec=30s` (2 min avant). Un tick **sans travail** (les 7 `node<N>.digest` égaux au digest du cache `:stable` de NODE3) sort en `0` **sans rien journaliser, sans SSH, sans sonde `/health` et sans prendre le verrou** partagé avec `alarm-chaos.sh` (INV-178) : le runner de panne n'est jamais gêné par un tick à vide. La découverte du leader Patroni (sondes `/health`) n'a lieu que s'il y a un passage à lancer. Les fichiers `/etc/systemd/system/alarm-cd-orchestrate.*` de NODE3 sont posés **à la main** : réinstaller après chaque modification (`docs/CD_DESIGN.md` §9bis). Pourquoi : release 1.9.5 (2026-11-09), PR #321 mergée à 14:37:03Z, CD Build fini à 14:37:32Z, mais ~1 h avant que node3 annonce la version : ~35 min de CI master avant `:stable` (INV-190), jusqu'à 5 min de pull, puis jusqu'à 17 min d'attente du tick. À 2 min, la découverte du leader faite jusque-là à chaque tick, avant même le verrou, aurait écrit 720 lignes de log par jour.
- **Codes de sortie** : `0` rien à faire / cooldown / `success` / `partial` ; `1` erreur de config ; `2` canary en échec (dont, depuis le 2026-11-13, aucun backend primary 60 s après une bascule ou un redémarrage, INV-197). Canary : `3` = prérequis non remplis, rien n'a été touché, ou leader Patroni sans bascule terminée (INV-187), ou aucun backend primary avant de redémarrer la cible (INV-197), pas redémarré. Le leader Patroni passe avant-dernier du passage (INV-197).
- **Pourquoi** : incident du 2026-10-29. alarm-1/alarm-2 (node4/node5, site Beaufort, non votants) hors ligne ~2 jours : le préflight SSH faisait `exit 1` sur 10.99.0.4, donc **aucun** nœud de prod n'a été mis à jour (4 versions jamais déployées), avec 1 h de cooldown à chaque tour. Exigence du propriétaire : « On doit pouvoir déployer sur les deux sites indépendamment sans être bloqué ». Le 2026-11-02 : alarm-1 sans réseau local, passé sur sa 4G de secours (VPN joignable, DNS mort) : `impossible de lire le digest distant` → canary en échec → chaîne arrêtée et 1 h de cooldown, Izeron bloqué par Beaufort.
- **Couverture** : Unit `tests/unit/test_cd_orchestrate.py` (vrai script, faux `ssh` / `docker` / canary) : Beaufort injoignable → 3, 2, 1 déployés, `partial`, puis 5, 4 au tick suivant → `success` ; node2 injoignable → aucun votant, 5 et 4 déployés ; canary node3 en échec → halt, `failure`, cooldown respecté puis retenté ; tous à jour → rien ; nœud en retard rattrapé après un `success`. RED sur le script d'origine (4 sur 5). Amendement 2026-11-02 : canary node4 pas prêt → 2, 1 déployés, `partial`, pas de cooldown, node4 au tick suivant ; canary node3 pas prêt → 5, 4 déployés, 2 et 1 reportés (votants) ; canary : SSH ou digest distant KO → exit 3 (`test_cd_canary_unready.py`). Amendement 2026-11-05 (7 nœuds, ordre `3 7 6 5 4 2 1`) : attentes ci-dessus réécrites dans ce nouvel ordre ; node6 et node7 injoignables (pas encore installés) → 3, 5, 4, 2, 1 déployés, `partial`, puis 7, 6 à leur arrivée ; canary `--node 6` / `--node 7` → cible `10.99.0.6` / `.7` (`test_cd_canary_unready.py`) ; `start-prod-node.sh` 1 à 7, `--no-deps patroni backend` pour 4 à 7 seulement (`test_start_prod_node.py`). Amendement 2026-11-09 : image promue pendant le passage (au canary node6) → second passage aussitôt, les 7 nœuds au nouveau digest, `success` ; promue à chaque passage → 3 passages puis arrêt (`test_cd_orchestrate.py`). Amendement 2026-11-09 (tick 2 min) : tous à jour sans `API_BASE` → aucun log, ni `ssh` ni `curl` ; tous à jour pendant que le runner de panne tient le verrou → aucun log ; passage à lancer sans `API_BASE` → découverte du leader ; timer `2min` / `30s` (`test_cd_orchestrate.py`). RED sur le script et le timer d'origine (3 sur 4).

### INV-190 [C] ✅ CD : `:stable` promu dès le build quand la PR a déjà été testée sur le même arbre ; jamais de retour arrière (2026-11-09)
Décision du propriétaire du 2026-11-09 : l'app attend toujours que le **serveur** annonce la version (INV-ANDROID-705) ; c'est la chaîne CD qui accélère (avec le tick orchestrateur 2 min, INV-167).
- **Deux déclencheurs** de `cd-promote-stable.yml`, sur un push master réussi : la **CI** (comme avant) et **CD Build** (images `:sha-<court>` publiées).
- **Après CD Build** → promotion **tout de suite** si, et seulement si :
  - le commit est le `merge_commit_sha` d'**exactement une** PR mergée (`GET /repos/{owner}/{repo}/commits/{sha}/pulls`) ;
  - il a **le même arbre** (`<sha>^{tree}`) que la tête de cette PR ;
  - sur cette tête, **chaque** check requis existe et est `completed` / `success` : `Tier 1 — Unit (logique pure)`, `Tier 1.5 — Mutation testing (logic pure, blocking)`, `Tier 2 — Integration (FastAPI TestClient + SQLite)`, `Tier 3 — E2E cluster (worker 1/2)`, `Tier 3 — E2E cluster (worker 2/2)`, `Prod config check (INV-076 — /api/test/* disabled)` (noms des jobs du workflow `CI`, `.github/workflows/pr.yml`).
  Le moindre doute (aucune PR ou plusieurs, arbre différent, check absent, en échec ou encore en cours, API GitHub en erreur, révision de `:stable` illisible) → **pas de promotion** : la CI master promeut, comme avant.
- **Après la CI** verte sur master → promotion, comme avant.
- **Jamais de retour arrière** (les deux déclencheurs) : un sha **égal** à la révision de `:stable` (label `org.opencontainers.image.revision` de `alarm-backend:stable`) **ou ancêtre** de celle-ci n'est pas (re)promu (sinon la CI master d'un commit précédent, finie après la promotion rapide du suivant, remettrait l'ancien code en `:stable`). Les promotions restent sérialisées (un seul groupe de concurrence) : la lecture de `:stable` et le re-tag ne se croisent pas.
- Toutes les vérifications existantes de la promotion restent : provenance (merge humain ou PR `ai:approved`), sha sur master, pas de revert, CD Build et CD Install Smoke verts sur le sha, images `:sha-<court>` présentes.
- **Limite connue** : un commit promu par ce chemin n'a pas encore passé la CI master (même arbre, mais Tier 1.5 ne mute sur une PR que les modules touchés, tout sur master). Si la CI master échoue ensuite, `:stable` reste sur ce commit : rien ne le retire automatiquement (le canary et le rollback auto, INV-167, restent le filet).
- **Pourquoi** : release 1.9.5 (2026-11-09) : PR #321 (fichier VERSION seul, CI entièrement verte sur sa tête, à jour de master) mergée à 14:37:03Z, CD Build du commit 25aabf4 fini à 14:37:32Z, puis ~35 min de CI master sur **le même arbre** avant `:stable`, puis le pull de NODE3 (5 min) et le tick orchestrateur (15 min) : ~1 h avant que node3 annonce la version aux téléphones.
- **Couverture** : Unit `tests/unit/test_promote_decision.py` (`.github/scripts/promote_decision.py`, fausses réponses API GitHub et git) : même arbre + checks requis verts → promotion ; arbre différent, un check en échec (chacun des 6), en cours, absent, aucune PR, PR qui n'a pas introduit le commit, `:stable` illisible → attente ; CI master → promotion ; CI master d'un sha déjà promu ou plus ancien que `:stable` → rien ; noms des checks présents dans `pr.yml`. RED sur l'ancien comportement (13 sur 17). Vérifié en lecture seule sur les vraies données : les commits de #321 (1.9.5) et #320 auraient été promus aussitôt. **Non testé** : le YAML du workflow (déclencheurs, job `decide`, `needs`), à vérifier au premier merge.

### INV-168 [H] ✅ Cluster : un replica bloqué est re-cloné automatiquement depuis le leader (2026-10-29, amendé le 2026-11-06)
Le backend du **leader** Patroni (`is_leader`), à chaque tick de `quorum_monitor_loop` (60 s), lit `GET {PATRONI_URL}/cluster`. Un membre est **bloqué** si son rôle est `replica` ou `sync_standby` **et** (`state != "streaming"` **ou** `timeline` < celle du leader). Bloqué **sans interruption** depuis ≥ `replica_reinit_after_minutes` (SystemConfig, défaut **10 min** ; absent, invalide ou ≤ 0 → 10) → le leader appelle `POST /reinitialize` `{"force": true}` sur l'API Patroni **de ce membre** (hôte et port pris dans son `api_url`). Patroni efface ses données et le re-clone depuis le leader.
- **Pas bloqué** : rattrapage normal (`streaming`, même timeline, `lag` qui baisse). Un membre **absent** de `/cluster` (injoignable) n'est pas traité : rien à re-cloner, et son compteur repart de zéro à son retour. `timeline` absente (champ optionnel) → seul le `state` compte.
- **Sain** (amendé le 2026-11-06) : replica (`replica` / `sync_standby`) **pas bloqué**, c.-à-d. `streaming` sur la timeline du leader. Le `lag` est **ignoré** : un replica qui suit le leader en `streaming` sur sa timeline prouve que le leader sert ses WAL. Mesure prod du 2026-11-06 : node4 et node7 (Beaufort, relayés par OVH) `streaming` en parfaite santé avec un lag stable de 32 à 64 Mo ; un seuil de lag les aurait jugés « pas sains » (email trompeur + 1 h de cooldown sur chaque membre bloqué).
- **Seuls les membres présents dans `GET /cluster`** comptent, pour « bloqué » comme pour « sain » (jamais `node_locations` ni un nœud éteint).
- **Garde-fous** :
  - jamais le leader ; rien du tout s'il n'y a pas de leader, si le leader n'est pas `running`, ou si le cluster est en pause (`patronictl pause` = maintenance manuelle) ;
  - au plus **un par membre et par heure** (y compris après un échec du POST) ;
  - **aucun replica sain** → **aucun** reinit (sans doute un problème côté leader ou réseau), email seul, au plus un par membre et par heure ;
  - **au moins un replica sain** → les replicas bloqués sont re-clonés **un à la fois**, quel que soit leur nombre (3 sur 5 inclus) : tant qu'un membre traité (reinit ou email de garde-fou) il y a **moins d'une heure** est encore bloqué dans `/cluster`, **aucun autre** reinit. Le suivant part dès que ce membre repasse `streaming` (ou quitte `/cluster`), ou à la fin de son heure de cooldown. Ordre : d'abord un membre jamais traité, puis le plus anciennement traité (un membre dont le POST échoue sans fin ne bloque pas les autres).
- **Chaque reinit** : `log_event("replica_reinitialized")` audité (`member`, `reason` = `state=… timeline=… leader_timeline=…`, `ok`, `error`) + email à `SystemConfig.alert_email`. Un échec HTTP (Patroni du membre injoignable, refus) est tracé dans l'audit et l'email ; ni lui ni un échec d'email n'arrêtent la boucle.
- **État en mémoire** du leader (série bloquée, cooldowns) : un changement de leader le remet à zéro (le nouveau leader observe 10 min avant tout reinit). Horloge réelle, pas `clock_now()` : `advance-clock` des tests E2E ne doit pas pouvoir re-cloner un replica.
- **Pourquoi** : incident prod du 2026-10-29. node4/node5 (Beaufort) éteints 2 jours ; au retour, Patroni les laisse en `running` (pas `streaming`) sur la timeline 253 alors que le cluster est en 256, lag croissant, log PostgreSQL `requested WAL segment ... has already been removed` (le leader a recyclé les WAL au-delà de `max_slot_wal_keep_size`). Ils ont servi des données vieilles de 2 jours jusqu'au `patronictl reinit alarm-cluster nodeX --force` manuel. Demande du propriétaire : détecter et re-cloner un tel replica automatiquement. **Amendement 2026-11-06** (incident) : redémarrage des 4 machines de Beaufort (node4 à node7) pendant qu'un déploiement CD relançait le leader d'Izeron (leader passé à node2, nouvelle timeline). Au retour, node4, node6 et node7 restent `running` (`requested WAL segment … has already been removed`) ; 3 replicas sur 5 bloqués → l'ancien garde-fou « plus de la moitié » refuse tout reinit (`plus de la moitié des replicas bloqués (node4), aucun reinit`) alors que node1 et node3 suivaient le leader à lag 0, donc que le leader allait bien. Un site qui héberge la majorité des replicas bloquait ainsi la reprise automatique à chaque redémarrage du site (reinit manuel demandé par le propriétaire). Décision du propriétaire : un replica sain suffit à innocenter le leader, re-clonage un par un.
- **Couverture** : Unit `tests/unit/test_replica_reinit.py` (logique pure `backend/app/logic/replica_reinit.py`, mutation 100 %) : bloqué 10 min → reinit, 9 min 59 → attente, rattrapage `streaming` → rien, vieille timeline → bloqué, leader jamais, pas de leader / leader pas `running` / pause → rien, cooldown 59 min 59 / 60 min. Integration `test_replica_reinit_inv168.py` (faux Patroni) : un seul POST `/reinitialize` vers node4 à 10 min, audité et envoyé par email ; POST en échec → audité, pas d'exception, pas de nouvel essai ; délai lu dans SystemConfig. Amendement 2026-11-06 : unit — incident (leader node2, node1/node3 sains, node4/node6/node7 bloqués, node5 absent de `/cluster`) → reinit de node4, puis rien tant qu'il est bloqué ; tous bloqués → aucun reinit, email ; un seul bloqué → reinit (inchangé) ; un à la fois (le suivant après retour en `streaming` ou fin de cooldown, jamais traité d'abord puis le plus ancien) ; seuls des replicas à ~64 Mo de lag sont `streaming` → les bloqués sont re-clonés (lag ignoré). Integration — incident : node4 à 10 min, rien tant qu'il est bloqué, node6 au tick où node4 repasse `streaming`, node7 ensuite ; seuls node4/node7 `streaming` avec 64 Mo / 32 Mo de lag → node1 re-cloné ; tous bloqués → email sans POST.

### INV-175 [H] ✅ Cluster : panneau « Résilience » — état réel des nœuds et scénarios de panne calculés (2026-10-30)
`GET /api/resilience` (admin) renvoie :
- `live` : pour chaque nœud de `node_locations` : `name`, `site`, `voter` (dans `etcd_voters`), `reachable` (son `/health` répond en 2 s, 200 ou 503), `role` et `state` Patroni, `version` (`/health.version`) ; pour chaque passerelle de `gateway_hosts` : `gateway_id`, `host`, `site` (lieu de son hôte), `registered` (déjà vue : `gateway_states` ou `modem_status:<id>`), `last_seen_s` (secondes depuis son dernier `report-state` ou `modem-status`, `null` si jamais vue) ; `leader` (nom du leader Patroni, `null` sinon) ; `etcd_voters_up` (voteurs dont le backend répond). Les sondes `/health` partent en parallèle (5 à la fois). Patroni injoignable → **503**.
- `whatif` : un résultat par scénario de `default_scenarios` : `scenario`, `label`, `untestable`, puis le verdict calculé par la logique pure `backend/app/logic/resilience.py` : `leader_possible`, `why`, `writable_from{site}`, `escalation_running`, `alarms_created{site}`, `sms_calls{site}` (sites qui ont une passerelle), `app_4g_reachable`, `app_izeron_lan`, `alerted_by`, `sentence` (phrase française).
- `GET /api/resilience/whatif?nodes=node4,node5&gateways=alarm-1` (admin) : même verdict pour un scénario libre (dialogue de confirmation de la panne contrôlée, PR 4). Nom inconnu ignoré.
- **Topologie** (SystemConfig, mêmes défauts sur une base déjà en service) : `node_locations` (INV-161), `etcd_voters` = `node1,node2,node3`, `relay_nodes` = `node3` (seul nœud qui relie les sites entre eux : sans lui, chaque site ne voit que ses propres nœuds), `gateway_hosts` = `onsite-2:node2,alarm-1:node4`. Nœud public (app en 4G) : node3.
- **Modèle** : deux nœuds se voient s'ils sont sur le même site ou si un relais est debout. Un leader est possible si un nœud debout voit **plus de la moitié** des voteurs (debout). Un site peut écrire si l'un de ses nœuds debout voit un tel nœud. Alarmes et SMS/appels d'un site = sa passerelle debout (hôte debout et passerelle non coupée) **et** site inscriptible. App 4G = node3 debout et OVH inscriptible ; app LAN Izeron = un nœud d'Izeron debout et Izeron inscriptible. `alerted_by` : cf. « Prévenu » ci-dessous (2026-11-10).
- **Scénarios par défaut** (table de vérité, cf. tests ; « Prévenu » amendé le 2026-11-10) : un nœud d'Izeron → tout OK, prévenu par INV-189 (ce nœud) ; OVH → leader oui, Beaufort aveugle (ni écriture, ni alarme, ni SMS), app 4G sans backend, prévenu par INV-170 (node4 + node5), INV-172 (alarm-1), INV-185 (Beaufort), INV-189 (node3 + node4 + node5) et INV-144 ; **Izeron entier → tout à faux, seul INV-170 (chaque nœud debout)**, marqué `untestable` (arrêt total, refusé par le propriétaire) ; Beaufort entier → seuls alarmes et SMS de Beaufort à faux, INV-172 (alarm-1), INV-185 (Beaufort), INV-189 (node4 + node5), INV-144 ; passerelle Izeron → alarmes et SMS d'Izeron à faux, INV-172 (onsite-2), INV-185 (Izeron), INV-144. `untestable` est **calculé** (aucun leader possible), donc suit la topologie : avec les voteurs `node1,node3,node6` (2ᵉ voteur cloud à la place de node2), Izeron entier garde un leader ; avec **4** voteurs `node1,node2,node3,node6`, non (majorité = 3).
- **7 nœuds (2026-11-05)** : `live.nodes[]` porte aussi `machine`, `retiring` et `group` (mêmes règles que `/api/cluster`, INV-161). node6/node7 (alarm-3/alarm-4) étant étiquetés `izeron`, le scénario « Izeron entier (node1 + node2 + node6 + node7) » reste `untestable` (2 voteurs sur 3 perdus) ; « Un nœud à Izeron » reste node1 ; Beaufort entier (node4 + node5) inchangé. **Limite** : node6/node7 sont physiquement à Beaufort jusqu'au déménagement ; le modèle les compte à Izeron (cf INV-161).
- **Scénario « Legacy débranché » (2026-11-05)** : clé `legacy_unplugged`, ajouté par `default_scenarios` dès que `retiring_nodes` n'est pas vide (après les sites, avant les passerelles), libellé « Legacy débranché (node1 + node2) : ce que corrige le jour du déménagement » = les nœuds `retiring` arrêtés. **Aujourd'hui** (voteurs node1, node2, node3) : node3 seul voteur, 1/3, **aucun leader**, `untestable` : c'est exactement ce que corrige le jour du déménagement (INV-090 : node6/node7 voteurs, node1/node2 retirés). Après le déménagement, `retiring_nodes` est vide et le scénario disparaît.
- **« Prévenu » = les emails que le code envoie aujourd'hui (2026-11-10, demande du propriétaire)** : `alerted_by` liste, pour le scénario, chaque email qui part réellement, avec ce qui le déclenche, qui l'envoie et après combien de temps, dans cet ordre (listes de nœuds dans l'ordre de la topologie, jointes par « + ») :
  - `email INV-170 « aucun leader » par <nœuds> après 3 min` : chaque nœud **debout** qui ne voit aucun leader l'envoie lui-même : tous les nœuds debout quand aucun leader n'est possible, sinon ceux d'un site isolé du leader (Beaufort quand OVH est arrêté) ;
  - puis, **avec un leader seulement** (contrôles de `quorum_monitor_loop` réservés au leader) : `email INV-172 « passerelle muette » <passerelles> par le leader après 10 min` (passerelle coupée, hôte arrêté ou site isolé du leader : plus de `report-state`) ; `email INV-185 « site sans modem » <Sites> par le leader après 10 min` (toutes les passerelles du site muettes : plus d'état de modem ; un site déjà sans modem a déjà eu le sien) ; `email INV-189 « nœud injoignable » <nœuds> par le leader après 10 min` (nœud arrêté ou isolé du leader, donc absent de `/cluster` : un email par nœud, puis « rétabli ») ; `email INV-053 « personne en ligne » par le leader après 15 min` (aucun chemin pour l'app) ; `email INV-144 « escalade sans appel » par le leader pendant une alarme` (un site sans SMS ni appel : escalade forcée à délai + 3 min, seulement si une alarme est en cours).
  - **Sans leader**, la phrase le dit : « Pas d'email du leader (INV-053, INV-144, INV-172, INV-185, INV-189) : aucun leader. » (Izeron entier, Legacy débranché : INV-189 ne peut pas partir, seul INV-170 prévient). Aucun nœud debout → « Prévenu : personne ».
  - **INV-085 n'y figure plus** : son contrôle compte les membres listés par `/cluster` ; un nœud arrêté ou isolé en disparaît (clé expirée), et la part de membres sains parmi les listés reste au-dessus de la moitié. Ce sont les emails INV-189 qui préviennent.
  - **Relais SMTP** : `smtp.gmail.com`, joint par chaque nœud sur son propre accès Internet (`wg0` ne porte que `10.99.0.0/24`) : aucun scénario de nœud ou de passerelle ne le coupe. Une coupure d'Internet d'un site n'est pas modélisée.
  - Non modélisés : SMS d'alerte de la passerelle (INV-171) et sonnerie de l'app hors connexion (INV-ANDROID-308).
- **Web** (onglet Tests, admin) : carte « Résilience », bouton « Rafraîchir », tableau des nœuds groupé (INV-161 : en-tête par groupe, « Legacy (Izeron) » en dernier ; Nœud = `machine · nom` + badge « bientôt obsolète », Lieu, Rôle, Voteur, Joignable, Version, Passerelle « vu il y a N s ») et tableau des scénarios (cellules vert/rouge + phrase) ; ligne non testable : badge « non testable : arrêt total » et note « 2ᵉ voteur cloud à prévoir » (Izeron entier) ou « corrigé le jour du déménagement » (`legacy_unplugged`). Chargée à l'ouverture de l'onglet.
- **Pourquoi** : demande du 2026-10-30 — voir d'un coup d'œil ce que le système fait encore quand un ou plusieurs nœuds tombent, avant de le tester en vrai (panne contrôlée, PR 4). Le panneau encode les trous connus du plan « pannes de nœuds » (G1-G4) ; « Prévenu » mis à jour le 2026-11-10 (INV-170, INV-172, INV-185, INV-189 en service).
- **Couverture** : Unit `tests/unit/test_resilience.py` (mutation 100 % ; « Legacy débranché » sans leader ; « Prévenu » de chaque scénario par défaut à 7 nœuds, site isolé du leader, sans leader, tout arrêté, deux passerelles sur un site) ; Integration `tests/integration/test_resilience_api.py` (faux Patroni et faux `/health` par `urlopen` ; 7 nœuds : `machine`, `retiring`, `group`, Izeron entier non testable, Beaufort inchangé, Legacy débranché sans leader) ; Playwright `TestResiliencePanel` (hors CI : nœuds groupés, note de la ligne Legacy).

### INV-176 [C] ✅ Panne contrôlée : garde-fous avant d'arrêter un nœud (2026-10-30)
`POST /api/chaos {nodes, mode, minutes}` (admin) demande l'arrêt réel d'un ou plusieurs nœuds pendant `minutes` ; `nodes` = nœuds (`node4,node5`) ou site (`site:beaufort` = ses nœuds de `node_locations`). La demande est **refusée (409, raison en français dans `detail`, affichée en snackbar)** par la logique pure `backend/app/logic/chaos.py::check_request`, dans cet ordre : `minutes` hors de 1..10 ; aucune cible ou nœud inconnu de `node_locations` ; **deux voteurs etcd ou plus** (voteurs = `etcd_voters`, INV-177) ; mode `netcut` sur `node3` (INV-177) ; **une alarme `active`, `escalated` ou `acknowledged`** ; **un test déjà `pending`, `claimed` ou `running`** (un seul test à la fois) ; cluster Patroni injoignable, membre de `node_locations` absent ou membre inconnu (autant de membres que `node_locations` : 7 depuis le 2026-11-05), leader absent ou pas `running`, un replica pas `streaming` (tous les membres sains). **Tant que node6/node7 n'ont pas rejoint le cluster, toute demande est donc refusée** (« membre absent du cluster Patroni : node6 ») ; pour tester avant leur arrivée, enregistrer `node_locations` sans eux (INV-161). `mode` ∈ `stop` (arrêt des conteneurs) | `netcut` (coupure du tunnel WireGuard `wg0` du nœud, cf. INV-178 ; refusé sur `node3`, INV-177).
- **Un seul test à la fois, garanti par la base** : index unique partiel `ux_chaos_one_unfinished` (`ON chaos_requests ((1)) WHERE status IN ('pending','claimed','running')`, SQLite et PostgreSQL). Deux `POST` simultanés qui passent tous deux le contrôle → un seul 201, l'autre 409 « un test est déjà en cours ».
- **Re-vérification au moment d'agir** : `GET /internal/chaos/next` (clé `X-Gateway-Key`, 503 `replica` hors leader) rejoue `check_request` (sans le critère « test en cours ») sur la plus ancienne demande `pending` : refus → `failed` (raison dans ses événements, audit `chaos_failed`), réponse `null` ; sinon `claimed` (`claimed_at`) par un `UPDATE … WHERE status = 'pending'` conditionnel : une demande annulée entre-temps n'est **jamais** réclamée (réponse `null`). La réponse porte `voters` (= `etcd_voters`) pour la sonde etcd du runner (INV-177). Une alarme apparue entre la demande et le tick du runner empêche donc le test.
- **Annulation** : `DELETE /api/chaos/{id}` (admin, audit `chaos_cancelled {status}`) : `pending` → `cancelled` (conditionnel : contre un claim simultané, un seul des deux gagne) ; `claimed` ou `running` → `cancel_requested = true` (bouton « Restaurer maintenant ») : le runner restaure aussitôt (INV-178), le statut reste `running` jusqu'à `restored`/`failed` ; statut terminé → 409.
- **Arrêt anticipé** : `GET /internal/chaos/{id}/abort` (clé passerelle, lecture seule) → `{"abort": raison | null}` : « annulé par l'admin » si `cancel_requested`, sinon « alarme en cours » si une alarme `active`, `escalated` ou `acknowledged` existe. Le runner l'interroge juste avant **chaque** `docker stop` (une raison → `failed` « `<raison>` : rien n'est arrêté sur `<nœud>` », les nœuds déjà listés relancés : une annulation ou une alarme entre la réclamation et l'arrêt n'arrête rien de plus) et, en `netcut`, juste avant **chaque** `cut` (« `<raison>` : rien n'est coupé sur `<nœud>` », les nœuds déjà coupés remis), puis toutes les 10 s pendant la fenêtre.
- **Expiration** (logique pure `chaos.py::is_stale`) : une demande `pending` jamais réclamée passe `failed` (événement `failed`, raison `timeout`) **30 min** après `created_at` ; une demande `claimed` ou `running` seulement **`minutes` + 5 min par nœud ciblé + 2 min** après `started_at` (sinon `claimed_at`) : couvre le pire cas d'un test **réussi** (après l'échéance, le runner attend jusqu'à 300 s chaque nœud, l'un après l'autre, plus les appels SSH et API ; `site:beaufort` en 2 min → 14 min) : un test réclamé tard ou un site lent à revenir n'est jamais marqué en échec pendant sa fenêtre ni sa restauration. Évaluée par `POST /api/chaos` et `GET /internal/chaos/next`, **sur le leader seulement** ; `GET /api/chaos` est en lecture seule (jamais d'écriture, jamais de 500 sur un replica en lecture seule).
- **Pourquoi** : demande du 2026-10-30 — tester en vrai, depuis l'onglet Tests, ce que fait le système quand un nœud tombe (table d'INV-175), sans jamais le faire pendant une alarme ni sur un cluster déjà dégradé.
- **Couverture** : Unit `tests/unit/test_chaos.py` (mutation 100 %) ; Integration `tests/integration/test_chaos_inv176.py` (faux Patroni par `urlopen`).

### INV-177 [C] ✅ Panne contrôlée : jamais deux voteurs etcd (Izeron entier refusé, coupure d'OVH refusée) (2026-10-30)
Les voteurs etcd sont ceux de `etcd_voters` (SystemConfig, INV-175 ; défaut `node1,node2,node3`), lus à chaque demande : jamais une liste codée en dur. Une cible qui en contient **deux ou plus** est refusée avec la raison « `node1, node2` : quorum etcd perdu — refusé » : le site Izeron entier (`site:izeron` = node1 + node2, et node6 + node7 depuis le 2026-11-05) n'est **jamais** testable (arrêt total, cf. INV-175 `untestable`) ; avec 4 voteurs (`node1,node2,node3,node6`), `node2,node6` est refusé de même. Un seul voteur (ex. `node3` = « OVH arrêté », ou un nœud d'Izeron) reste permis, de même qu'un non votant seul (`node6`). « Legacy débranché » (`node1,node2`) est donc refusé aussi (2 voteurs) : il se lit dans le panneau Résilience (INV-175), il ne se teste pas.
- **Coupure réseau d'OVH refusée en V1 (2026-10-30)** : `netcut` sur `node3` est refusé par `check_request` (« coupure réseau de node3 refusée : le runner et l'interface web y vivent ») **et** par le runner lui-même (`alarm-chaos.sh` → `failed` sans aucun SSH, même si une demande avait passé le filtre) : le runner `alarm-chaos.timer` vit sur NODE3 et n'atteint les cibles que par `wg0` ; couper OVH le rendrait aveugle (plus de suivi du leader ni d'événements, interface web servie par NODE3 disparue). `stop node3` reste le test « OVH arrêté ».
- **Coupure d'un nœud passerelle = site aveugle (risque assumé, 2026-10-30)** : `netcut` d'un nœud qui héberge une passerelle (`gateway_hosts`, INV-175 ; défaut `node2` = onsite-2, **Izeron**, et `node4` = alarm-1, **Beaufort**) rend ce site **aveugle pendant T min : ni alarme par contact sec, ni SMS, ni appels** (la passerelle joint les backends par leurs IP `10.99.0.x`, via `wg0`, y compris celui de son propre hôte ; elle met en file). Permis (c'est le scénario à observer : alertes INV-144/172), mais dit en toutes lettres dans le `confirm()` de l'interface (INV-179). `node2` est en plus un voteur etcd (un seul : permis). `node5` n'héberge aucune passerelle.
- **État réel d'etcd** : compter les voteurs de la cible ne suffit pas (un etcd déjà tombé + un voteur arrêté = quorum perdu). Juste avant d'arrêter quoi que ce soit, le runner sonde depuis NODE3 `http://<IP WG du voteur>:2379/health` (`curl -sf --max-time 3`) pour **chaque** voteur de `voters` ; un voteur pas sain (ou inconnu du runner) → `failed` avec la raison, **rien n'est arrêté**. Le backend ne connaît pas l'état d'etcd (aucune sonde etcd côté backend) : ce contrôle est fait par le runner seul, au dernier moment.
- **Web** : dans la carte « Panne contrôlée », l'option d'un site qui contient au moins deux voteurs (d'après `live.nodes[].voter` de `/api/resilience`) est désactivée avec la mention « quorum » ; les deux modes `stop` et `netcut` (« Coupure réseau (wg0) ») sont sélectionnables.
- **Pourquoi** : décision du propriétaire (2026-10-30) : le scénario « 2 nœuds d'Izeron » est refusé et documenté, pas testé.
- **Couverture** : Unit `tests/unit/test_chaos.py` (dont 4 voteurs) et `tests/unit/test_alarm_chaos.py` (etcd d'un voteur KO → `failed`, aucun arrêt) ; Integration `test_chaos_inv176.py` (409 sur `site:izeron` à 7 nœuds, `node6` seul accepté, node6 absent de Patroni → 409, voteurs lus dans `etcd_voters`).

### INV-178 [C] ✅ Panne contrôlée : restauration garantie, armée sur la cible avant l'arrêt ou la coupure (2026-10-30)
Le runner `scripts/alarm-chaos.sh` (timer `alarm-chaos.timer` toutes les minutes sur NODE3, livré désactivé, installé à la main comme l'orchestrateur) :
- prend le **verrou partagé avec le CD** (`/var/lib/alarm/cd/orchestrate.lock`, `flock -n`) ; verrou pris (déploiement en cours) → **ne réclame rien** et sort ; `alarm-cd-orchestrate.sh` utilise le même fichier par défaut (plus `/run/alarm-cd-orchestrate/lock`, qui disparaissait avec le oneshot) : pas de déploiement pendant un test (l'orchestrateur sort 0 sans déployer), pas de test pendant un déploiement ;
- connaît l'IP WireGuard de node1 à node7 (comme `alarm-cd-orchestrate`, INV-167 ; un nœud hors de cette liste → `failed` sans SSH) ;
- découvre le leader Patroni (`/health` `role=primary`), `GET /internal/chaos/next`, envoie `started {leader_before}`, puis sonde etcd sur chaque voteur (INV-177) ;
- fixe **une échéance absolue** (maintenant + T min) : la fenêtre se termine à l'échéance quelle que soit la durée des appels, et chaque restauration armée tire à l'échéance + 60 s ;
- pour chaque nœud cible : `CIDS = docker ps -q --filter label=com.docker.compose.project=<node>` par SSH (`alarm@10.99.0.N`), puis, dans une seule commande SSH, `docker update --restart=always $CIDS` (Docker relance au démarrage du daemon un conteneur `always` même arrêté à la main, pas un `unless-stopped` : un **redémarrage de l'hôte** pendant la fenêtre ne laisse pas le nœud arrêté) et **arme la restauration sur la cible elle-même** (`setsid nohup sh -c 'sleep <échéance+60> ; docker start $CIDS ; docker update --restart=unless-stopped $CIDS' alarm-chaos-<id>`, détaché de la session SSH). La commande n'imprime `ARMED` que si ce processus vit encore 1 s après son lancement, puis une **nouvelle** session SSH vérifie qu'il a survécu à la fermeture de la première (`pgrep`, cf. systemd-logind `KillUserProcesses`). Sans `ARMED` ou sans survivant → `failed`, **rien n'est arrêté** sur ce nœud. Seulement ensuite `docker stop $CIDS`. Le nœud redémarre donc même si le runner, NODE3 ou le réseau disparaissent pendant le test ;
- pendant la fenêtre, toutes les 10 s : lit `/api/cluster` (n'importe quel nœud joignable) et envoie `leader_changed {from, to}` à chaque changement ; lit `GET /internal/chaos/{id}/abort` : une raison (annulation admin, alarme survenue) → événement `early_restore {reason}` (audité) et **restauration immédiate** ;
- à l'échéance (ou plus tôt) : `docker start $CIDS && docker update --restart=unless-stopped $CIDS && pkill -f '[a]larm-chaos-<id>$'` : un nœud relancé **désarme** sa restauration armée (sinon elle tirerait à l'échéance + 60 s, en plein test suivant sur ce nœud) ; motif lié à l'id et ancré en fin de ligne (`alarm-chaos-7` ne touche jamais `alarm-chaos-70`), filet laissé armé si `docker start` échoue. Attend ≤ 300 s que `/health` du nœud réponde 200 **et** que son membre Patroni soit `running` s'il est leader, `streaming` sinon (un replica `running` sans `streaming` est bloqué, cf. INV-168) → `restored {leader_after, restore_seconds}` ; sinon `failed {reason}`.
- juste avant **chaque** `docker stop` : relit `GET /internal/chaos/{id}/abort` ; une raison → `failed` « `<raison>` : rien n'est arrêté sur `<nœud>` » (INV-176) ;
- Tout échec après la réclamation (`docker ps`, armement, arrêt anticipé avant un `stop`, `stop`, `start`, nœud pas sain) → `failed`, et **tous** les nœuds dont les conteneurs ont été listés sont relancés aussitôt (même commande que ci-dessus : `docker start` + `unless-stopped` + désarmement, sans effet sur un conteneur déjà en marche) : un `docker stop` partiel est couvert.
- SSH : `BatchMode=yes`, `ConnectTimeout=10`, `ServerAliveInterval=5`, `ServerAliveCountMax=3` (un trou réseau en pleine commande échoue en ~15 s au lieu de geler le runner et le verrou CD).
- Les événements sont envoyés au leader courant (redécouvert si le POST échoue : le nœud arrêté peut être l'ancien leader).
- **Mode `netcut` — coupure réseau (2026-10-30)** : le runner n'arrête rien ; après la sonde etcd (INV-177), pour chaque nœud cible il lance par SSH (mêmes options) `sudo -n /usr/local/bin/alarm-chaos-net cut T×60` (helper root `scripts/alarm-chaos-net.sh`, posé en 755 par `infra/onsite/install.sh`, donc déployé partout par `alarm-host-sync` ; `sudo -n` autorisé par l'alias `ALARM_CHAOS` de `infra/onsite/sudoers/alarm-cd`). Le helper, **sur la cible et en root** :
  - refuse (exit 2, rien ne se passe) des arguments invalides : sous-commande inconnue, argument manquant ou en trop, secondes qui ne s'écrivent pas en **décimal strict** (`^[1-9][0-9]{1,2}$` : un zéro en tête comme `0777` serait lu en octal par bash mais en décimal par systemd, 13 min au lieu de 8 ; pas de nombre géant qui déborderait 64 bits) ou hors de **60..720** ;
  - refuse (exit 1, **rien n'est armé ni coupé**) si `wg-quick@wg0` n'est pas l'unit active (wg0 monté à la main ou par un autre gestionnaire, nœud déjà coupé) : `systemctl stop` d'une unit inactive réussirait sans rien couper (faux positif silencieux) ;
  - arrête d'éventuels timers `alarm-chaos-cut` / `alarm-chaos-restore` restants et efface l'échec d'une restauration précédente (`reset-failed`) ;
  - **arme d'abord la restauration** : timer transitoire `alarm-chaos-restore` (`systemd-run --on-active=S --timer-property=AccuracySec=1s --unit=alarm-chaos-restore`, **sans** `--collect` : un échec final reste visible dans `systemctl --failed`) qui lance une **boucle bornée** : jusqu'à **10** tentatives, **30 s** d'écart, chaque tentative et l'issue écrites au journal (`journalctl -u alarm-chaos-restore`). Une tentative = `wg-quick@wg0` déjà l'unit active → réussie, rien n'est touché (jamais de `wg-quick down` sur un tunnel remis) ; sinon `wg-quick down wg0` (échec ignoré) puis `systemctl start wg-quick@wg0` : une interface `wg0` **résiduelle** (`ExecStop` en erreur pendant la coupure) ferait échouer chaque `start` sur « wg0 already exists » ; armement en échec ou timer pas actif juste après (preuve de l'armement) → exit 1 **sans couper** ;
  - puis seulement arme la coupure `alarm-chaos-cut` (`systemd-run --on-active=5 --timer-property=AccuracySec=1s --unit=alarm-chaos-cut --collect systemctl stop wg-quick@wg0`) : 5 s de délai, car la session SSH du runner passe elle-même par `wg0`. `AccuracySec=1s` : la précision par défaut d'un timer systemd est 1 min (coupure et retour à ±60 s) ;
  - imprime `ARMED` (seule sortie standard) et sort 0.
  La restauration est donc un **timer root de la machine coupée**, indépendant de NODE3, du VPN et de toute session SSH. `alarm-chaos-net restore` (une tentative, la même) annule une coupure pas encore exécutée **ou déjà déclenchée** (`alarm-chaos-cut.timer` et `.service` arrêtés), remet `wg0` tout de suite, le **revérifie 2 s plus tard** (un `stop` déjà en file passe pendant ce délai ; sans cela, une restauration anticipée ~5 s après l'armement pouvait désarmer le filet d'un nœud ensuite coupé), puis **désarme** le timer de restauration s'il attend encore (`systemctl stop alarm-chaos-restore.timer` : sinon il tirerait à l'échéance, en plein test suivant ou maintenance sur ce nœud) ; `wg0` pas remis → exit ≠ 0 et timer **laissé armé** en filet.
  - **Runner** : le nœud est noté « touché » **avant** l'appel `cut` ; sortie sans `ARMED` (helper qui refuse, armement KO, SSH interrompu après l'armement) → `failed` « cut KO ou SSH interrompu » et `sudo -n … restore` tenté sur ce nœud et sur ceux déjà coupés (filet : leur timer). Juste avant **chaque** `cut` : `abort` relu (INV-176), une raison → `failed`, ce nœud jamais coupé, ceux déjà coupés remis. **Coupure prouvée** : environ **20 s** après le dernier `cut` (15 s après l'exécution de la coupure), une sonde depuis NODE3 de `/health` de chaque nœud coupé (`curl --max-time 3`, sans `-f` : toute réponse HTTP, même 503, compte) ; un nœud qui **répond encore** → `failed` « coupure non effective sur `<nœud>` » et `restore` sur chaque nœud coupé (sans elle, un `systemctl stop` sans effet passait pour un `restored` en 0 s). Pendant la fenêtre : suivi du leader et `abort` comme pour `stop`. **Arrêt anticipé** (`abort`) : `restore` tenté sur chaque nœud coupé ; joignable (coupure pas encore partie, autre chemin) → `wg0` remis tout de suite ; injoignable (cas normal : le runner passe par `wg0`) → journalisé, le timer de la cible remet `wg0` à l'échéance et l'attente de santé court jusqu'à l'**échéance + 300 s** (`restore_seconds` compté depuis l'échéance, jamais négatif). À l'échéance, rien à faire à distance (le tunnel est coupé, le timer restaure) : `wait_healthy` (`/health` 200 + membre Patroni `running` si leader, `streaming` sinon, ≤ 300 s) → `restored` / `failed` (et `restore` tenté). Une fois chaque nœud de nouveau sain (fin normale ou anticipée), le runner y lance `restore` (`wg0` déjà actif : rien n'est touché) pour **désarmer** son timer s'il attend encore ; un échec de ce désarmement est journalisé, sans changer le résultat. Un site (`site:beaufort`) = chaque nœud du site reçoit son `cut`. Mode inconnu du runner → `failed`.
- **Sudoers posé par `install.sh`** : le fichier du dépôt est copié dans un fichier temporaire root, `visudo -c -q -f` lint **cette copie**, et c'est elle qui est posée en `/etc/sudoers.d/alarm-cd` (mode 440) : ce qui est linté est ce qui est posé. visudo refuse → **avertissement, seule l'étape sudoers est sautée** (l'ancienne reste en place) et le reste d'`install.sh` continue (une sudoers cassée bloquerait tout `sudo` sur le nœud ; un `exit` paralyserait le CD hôte). Idempotent (`cmp` avant `install`). Modèle de confiance : la source est le checkout `/opt/alarm` (owner `alarm`), comme pour `alarm-host-sync` qui installe déjà depuis ce checkout des scripts exécutés en root (`/usr/local/bin/alarm-host-sync`, `install.sh` lui-même) : poser la sudoers depuis ce checkout n'élargit pas ce que `alarm` peut déjà faire exécuter en root.
- **Pourquoi** : un test de panne qui ne se restaure pas est une panne. La seule garantie acceptable est locale à la machine arrêtée ou coupée, et doit survivre à son redémarrage.
- **Couverture** : Unit bash `tests/unit/test_alarm_chaos.py` (vrai script, faux `ssh` / `curl`, `MINUTE_SECONDS=1`) : rien en attente → aucun SSH ; `stop node6` → chaque SSH vers `10.99.0.6`, `restored` ; `stop node4` → sondes etcd, `restart=always` + armement (`ARMED`) puis vérification du survivant **avant** `docker stop`, puis `docker start` + `unless-stopped` + désarmement (`pkill` de ce seul id : un vrai `alarm-chaos-<id>0` survit), POST `started` puis `restored`, options SSH ; verrou pris → pas de `next` ; SSH en échec → `failed` ; armement sans `ARMED` → `failed`, aucun `docker stop` ; `docker stop` KO sur node5 → node4 et node5 relancés ; etcd d'un voteur KO → `failed`, aucun arrêt ; arrêt anticipé (`abort`) → `early_restore` puis `restored` (dernière commande SSH = relance + désarmement) avant l'échéance ; `abort` avant le premier ou le second `docker stop` d'un site → `failed`, ce nœud jamais arrêté, nœuds listés relancés ; replica `running` non `streaming` → `failed` ; `netcut node4` → un seul SSH `sudo -n /usr/local/bin/alarm-chaos-net cut 180` (options SSH), aucun `docker`, puis `restored` ; `netcut site` → un `cut` par nœud ; `netcut node3` → `failed` sans SSH ; etcd d'un voteur KO en `netcut` → aucun SSH ; `cut` sans `ARMED` ou SSH interrompu sur le 2ᵉ nœud → `restore` sur les deux, `failed` ; arrêt anticipé `netcut` → `restore` par SSH puis `restored` avant l'échéance ; cible injoignable → attente jusqu'à l'échéance + délai, `restored` ; nœud jamais sain → `failed` et `restore` ; fin normale → `restore` (désarmement) après le retour, avant `restored` ; `abort` avant le premier ou le second `cut` d'un site → `failed`, ce nœud jamais coupé, nœud déjà coupé remis ; `/health` qui répond encore après la coupure → `failed` « coupure non effective », `restore`. Unit bash `tests/unit/test_alarm_chaos_net.py` (vrai helper, faux `systemd-run` / `systemctl`) : `wg-quick@wg0` actif vérifié d'abord, restauration (`AccuracySec=1s`, sans `--collect`) armée et prouvée **avant** la coupure, `ARMED` ; boucle de restauration rejouée (3ᵉ tentative OK → 2 attentes de 30 s, `wg-quick down wg0` avant chaque `start` ; jamais OK → 10 tentatives, `ECHEC`, exit 1 ; `wg0` résiduel : `start` qui échoue tant que l'interface existe → remis à la 1ʳᵉ tentative ; unit déjà active → aucun `down`, aucun `start`) ; secondes invalides (dont `0777`, `01320`, 2⁶⁴+100) → exit 2 sans rien armer ; `wg-quick@wg0` inactif → exit 1, rien ; armement en échec ou timer pas actif → aucune coupure ; `restore` (coupure annulée, `wg0` remis puis timer désarmé ; `wg0` résiduel → `down` puis `start` ; `start` KO → exit ≠ 0, timer laissé armé). `tests/unit/test_cd_orchestrate.py` : verrou par défaut `${STATE_DIR}/orchestrate.lock` tenu → l'orchestrateur sort 0 sans déployer. Smoke `cd-install-smoke.yml` : unit/timer installés, timer désactivé ; helper installé, `visudo -c -f` sur la sudoers, `sudo -n` par `alarm` autorisé sur le helper, vrai `systemd-run` (timers à `AccuracySec=1s`, coupure puis restauration d'une unit factice, refus si l'unit est inactive).

### INV-179 [H] ✅ Panne contrôlée : auditée, résultat visible (2026-10-30)
Table `chaos_requests` (`id, created_at, requested_by → users SET NULL, nodes, mode, minutes, status pending|claimed|running|restored|failed|cancelled, claimed_at, started_at, restored_at, cancel_requested, events JSON`), migration idempotente `_migrate_chaos_requests` (SQLite et PostgreSQL, colonne `cancel_requested` et index `ux_chaos_one_unfinished` ajoutés s'ils manquent). `POST /internal/chaos/{id}/events {kind started|leader_changed|early_restore|restored|failed, details, ts?}` ajoute l'événement et fait avancer `status` / `started_at` / `restored_at` (`restored` et `failed` ferment la fenêtre ; 503 `replica` hors leader). Sur une demande **terminée** (`restored`, `failed`, `cancelled`), l'événement est **seulement ajouté** à `events` : ni statut, ni horodatage, ni audit (le `restored` d'un runner arrivé après l'expiration `timeout` ne contredit pas `chaos_failed`). Événements audités (`AUDITABLE_EVENTS`, `AuditEvent`) : `chaos_requested`, `chaos_cancelled`, `chaos_started`, `chaos_early_restore`, `chaos_restored`, `chaos_failed` (`leader_changed` reste dans `events` seulement).
- `GET /api/chaos` (admin, lecture seule) : les **10 dernières** demandes (avec `cancel_requested`), chacune avec `alerts` = les `AuditEvent` de la fenêtre `started_at` → `restored_at` (ou maintenant) hors `chaos_*` : ce que le système a réellement fait pendant la panne (escalade, emails, reinit…).
- **Web** (onglet Tests, admin, carte « Panne contrôlée » après « Résilience ») : cible (nœuds `alarm-1 · node4 (Beaufort)`, `onsite-1 · node1 (Izeron) — bientôt obsolète`, et sites `Site Beaufort (node4, node5)` d'après `/api/cluster`, INV-161 ; options groupées par `<optgroup>` : un par groupe d'affichage (Izeron, Beaufort, OVH, …, « Legacy (Izeron) »), puis « Sites entiers »), mode, minutes 1-10, bouton danger ; `confirm()` avec la phrase de `GET /api/resilience/whatif?nodes=…` (texte fixe si l'appel échoue), précédée en `netcut` d'un avertissement par nœud cible qui héberge une passerelle (`live.gateways[].host` de `/api/resilience`) : « site X aveugle pendant T min : ni alarme par contact sec, ni SMS, ni appels » (INV-177) ; liste des passerelles **inconnue** (dernier `/api/resilience` en échec, ou sans passerelle) → en `netcut`, quelle que soit la cible, l'avertissement générique « passerelles inconnues : couper un nœud passerelle rend son site aveugle (contact sec, SMS, appels) » (jamais un `confirm()` muet) ; historique (cible, mode, statut, leader avant → après, nb d'alertes, durée de restauration, « Annuler » si `pending`, « Restaurer maintenant » si `claimed` ou `running`) rafraîchi en direct (`LIVE_WATCH['/api/chaos']`, admin seulement) ; les nouveaux événements du test en cours s'ajoutent au journal de test.
- **Pourquoi** : le propriétaire veut voir, depuis l'onglet Tests, ce qui s'est passé pendant la panne et qui l'a déclenchée.
- **Couverture** : Integration `test_chaos_inv176.py` (statuts, horodatages, audit, alertes de la fenêtre, 503 replica sur `next` et `events`, `GET` en lecture seule sur un replica, `restored` tardif sur une demande expirée : reste `failed`, aucun `chaos_restored`) ; Playwright `test_frontend.py::TestChaosConfirm` (hors CI, `/api/resilience`, `/api/cluster` et `whatif` simulés) : `netcut node4` → le `confirm()` nomme Beaufort aveugle ; `stop node4` → aucun avertissement ; `/api/resilience` en échec → avertissement générique.

### INV-172 [H] ✅ Passerelle muette depuis 10 min → email et audit, une fois par épisode (2026-10-30)
Le backend du **leader** Patroni, à chaque tick de `quorum_monitor_loop` (60 s, juste après le contrôle INV-085, **hors de l'event loop** : `asyncio.to_thread` avec sa propre session, comme le snapshot INV-085 et INV-168 — la lecture DB et l'envoi SMTP, timeout 10 s, ne doivent pas geler `/health` ni les heartbeats), lit `gateway_states` (une ligne par passerelle, `last_seen` posé par chaque `POST /internal/alarms/report-state`, INV-120). Une passerelle est **muette** si `clock_now() − last_seen ≥ 10 min` (`GATEWAY_SILENCE_THRESHOLD`, soit 120 polls manqués à 5 s ; la fenêtre de vivacité de 15 s d'INV-120 ne change pas), et au plus **7 jours** (au-delà : passerelle retirée, ignorée, cf. plus bas). Pour chaque passerelle qui **devient** muette, le leader :
- envoie à `SystemConfig.alert_email` l'email `[Alarme Murgat] Passerelle <gateway_id> (<Site>) muette depuis N min` (site de la ligne, `default_site` si absent, capitalisé) : dernier rapport (`last_seen` UTC), conséquence (**ce site ne peut plus déclencher d'alarme par contact sec ni émettre de SMS ou d'appel** — pas de secours entre sites, décision du propriétaire ; ses SMS et appels attendent son retour, INV-145), vérifications à faire sur le nœud passerelle (`systemctl status alarm-sms-gateway`, modem USB, alimentation, WireGuard) ;
- écrit `log_event("gateway_silent")`, **audité** (`gateway_id`, `site`, `last_seen`, `silent_minutes`).
- **Un email par épisode** : les passerelles muettes déjà signalées sont tenues dans `SystemConfig gateway_silence_emailed` (liste JSON de `gateway_id`, créée au premier usage, pas de migration ; partagée par les leaders successifs). Tant qu'une passerelle y est, pas de nouvel email ; elle en sort dès qu'un rapport la ramène sous le seuil, et un nouveau silence redonne un email.
- **Pas de suppression au démarrage** : le premier contrôle d'un processus (déploiement, bascule de leader) traite les passerelles déjà muettes comme les autres. `gateway_silence_emailed` étant persisté, une passerelle déjà signalée par un leader précédent n'est pas re-signalée ; une passerelle devenue muette pendant un redémarrage ou une bascule est signalée au premier tick du nouveau leader. (Correctif de relecture du 2026-10-30 : la première version inscrivait sans email les passerelles déjà muettes au premier tick du processus et perdait ainsi l'alerte d'une passerelle tombée juste avant un déploiement ou une bascule.)
- **Passerelle retirée** : une ligne dont `last_seen` a **plus de 7 jours** (`GATEWAY_RETIRED_AFTER` : passerelle renommée ou retirée, ligne de test) est ignorée : ni email ni audit, et elle sort de `gateway_silence_emailed`. Exactement 7 jours : encore prise en compte. Une ligne obsolète plus récente coûte un email, une seule fois.
- **`alert_email` vide** → rien d'envoyé (warning), mais l'inscription et l'audit sont faits (pas de rafale quand l'adresse est configurée plus tard). Horloge : `clock_now()`, comme `last_seen`.
- **Passerelle sans Arduino** (INV-194, 2026-11-12) : elle rapporte `no_sensor` toutes les 5 s, donc n'est pas muette ; seule une passerelle qui n'envoie plus rien l'est.
- **Pourquoi** : plan du 2026-10-30 (trou G3). Une passerelle à l'arrêt (service, modem débranché, alimentation, WireGuard) n'alertait personne : les SMS et appels de son site s'empilaient en silence et son contact sec n'était plus lu. INV-158 ne couvre que le modem (« inconnu » après 180 s) ; ici c'est la passerelle entière, par email, comme les autres pannes d'infrastructure (INV-085, INV-168).
- **Couverture** : Unit `tests/unit/test_gateway_silence.py` (logique pure `backend/app/logic/gateway_silence.py`, mutation 100 %) : 9 min 59 → rien, 10 min 00 et 10 min 01 → signalée, déjà signalée → rien, retour puis nouveau silence → signalée de nouveau, deux passerelles dont une muette, liste vide, 7 jours → signalée, 7 jours + 1 s → ignorée et retirée de `emailed`. Integration `test_gateway_silence_inv172.py` : report-state à T0 → contrôle à T0 + 11 min → 1 email (sujet, destinataire, `last_seen`, conséquence, vérifications) + 1 audit, clé persistée ; 2e contrôle → rien ; nouveau report-state → réarmée, nouveau silence → 2e email ; premier contrôle d'un **processus neuf** (sous-processus sur la même base : aucun état de module hérité des tests précédents ni du tick de fond) avec une passerelle déjà muette depuis 30 min → 1 email, puis le contrôle suivant, dans le processus de test, n'en renvoie pas (clé partagée) ; le tick de `quorum_monitor_loop` appelle le contrôle hors de l'event loop. Dans le reste de la suite tier 2, ce contrôle est neutralisé dans le tick de fond (`tests/integration/conftest.py`) : en SQLite `is_leader` est posé et des tests laissent des lignes `gateway_states` avec une horloge avancée.

### INV-173 [H] ✅ Relais d'écriture d'un replica : le leader Patroni d'abord, connexion bornée à 1,5 s (2026-10-30)
Quand un replica relaie une écriture `/api/*` (INV-044) ou un heartbeat (INV-043) vers le leader, il lit d'abord `GET {PATRONI_URL}/cluster` sur son Patroni local et essaie **en premier** le peer de `PEER_TEST_URLS` dont l'hôte (`urlsplit(url).hostname`) est celui du membre `role = leader` (`host`, soit l'`ADVERTISE_HOST` du nœud, son IP WireGuard) ; les autres peers suivent dans l'ordre configuré (`logic/write_route.py::leader_first`). Patroni injoignable, pas de leader, ou leader dont l'hôte ne correspond à aucun peer → ordre configuré inchangé. Timeouts : `httpx.Timeout(5.0, connect=1.5)` pour les écritures, `httpx.Timeout(3.0, connect=1.5)` pour le heartbeat ; `GET /cluster` borné à **1 s** (Patroni local : un Patroni gelé coûte au plus 1 s par relais, puis ordre configuré).
- **Connexion 1,5 s (arbitrage)** : elle absorbe une perte de SYN (retransmission Linux à 1 s) tant que le RTT reste < 0,5 s ; la retransmission suivante part à 3 s, donc ni 1,5 s ni 3 s n'absorbent deux pertes de suite (même probabilité p², le client réessaie : rotation de l'app, nouvel essai web). Un délai plus long ne servirait qu'à un RTT de 0,5 à 2 s (4G saturé) et rendrait un peer mort plus cher quand Patroni est injoignable.
- **Pourquoi** : plan du 2026-10-30 (trou G4). Les peers étaient essayés dans l'ordre fixe du fichier d'env, avec 5 s (écritures) ou 3 s (heartbeat) par peer **mort** : un nœud éteint en tête de liste coûtait 5 s à chaque login ou action web depuis OVH, plus que le délai de l'app. Avec le leader en tête et une connexion bornée à 1,5 s, un peer mort n'est en pratique plus essayé et coûte au plus 1,5 s.
- **CI / dev** : tous les membres Patroni ont le même hôte (`host.docker.internal`) ou `PEER_TEST_URLS` est vide → ordre inchangé.
- **Couverture** : Unit `tests/unit/test_write_route.py` (`leader_first`, mutation 100 %) : leader en 2e position remonté, déjà en tête, hôte inconnu des peers, pas de leader, cluster `None`, hôtes identiques (CI). Integration `test_write_route_inv173.py` : `_PEER_URLS = [mort, leader]` + faux Patroni → le **premier** appel HTTP du relais d'écriture (resp. du heartbeat) vise le leader, avec les timeouts ci-dessus, et le peer mort n'est pas essayé ; Patroni injoignable → ordre configuré.

### INV-174 [M] ✅ Passerelle : dernier leader mémorisé, autres nœuds sondés à 3 s (2026-10-30)
`_find_primary_url` (gateway) garde en mémoire du processus la dernière URL dont `/health` a répondu `role = primary`, et la sonde **en premier** à l'appel suivant (`modem_health.probe_order(cached, urls)` : l'URL mémorisée d'abord, sans doublon, ignorée si elle n'est plus dans la liste), puis les autres dans l'ordre `NODE1_URL … NODE7_URL` (`NODE4_URL` à `NODE7_URL` facultatifs : vides par défaut, ignorés ; `infra/onsite/install.sh` les ajoute s'ils manquent, 10.99.0.4 à .7). Le rôle est **toujours revérifié** (un ancien leader devenu replica n'est pas réutilisé) ; si l'URL mémorisée ne répond plus `primary`, balayage complet puis nouvelle mémorisation (ou oubli si aucun primary). La sonde `/health` de l'URL mémorisée garde `HTTP_TIMEOUT_SECONDS` (défaut **10 s**, inchangé : la tolérance d'avant INV-174) ; seules les **autres** URL de la recherche sont sondées à **3 s** (`PRIMARY_PROBE_TIMEOUT_SECONDS`, constante de `config.py`, indépendante du fichier d'env ; choix fait par `modem_health.probe_timeout`). `HTTP_TIMEOUT_SECONDS` reste aussi celui des échanges de données (`GET`/`POST` vers le primary) et du `HealthMonitorThread` : une pointe de latence de moins de 10 s sur un primary vivant (secours 4G saturé, base chargée, `/health` du leader ralenti) ne doit ni faire sauter un envoi (SMS, appels, report-state du contact sec, INV-120), ni déclencher le SMS d'alerte « backends ». (Correctif de relecture du 2026-10-30 : la première version sondait aussi le primary mémorisé à 3 s, et une pointe de 3 à 10 s sur son `/health` faisait sauter le poll en cours.)
- **Pas de verrou** : chaque thread (SMS, appels, contact sec, modem) fait sa propre recherche, en parallèle comme avant INV-174 ; deux écritures concurrentes de la mémoire coûtent au pire un balayage de plus (le rôle est toujours revérifié).
- **Pourquoi** : plan du 2026-10-30 (trou G4). Chaque requête de la passerelle (SMS en attente, appels, contact sec toutes les 5 s, état du modem) relançait la recherche du leader depuis le premier nœud de la liste : un nœud mort en tête coûtait 10 s par requête, et un nœud OVH en tête un aller-retour inutile. Avec le cache, une requête coûte un seul `/health` en régime établi ; après une bascule, l'ancien primary mort coûte 10 s et chaque autre nœud mort 3 s, **une fois, dès qu'un primary répond**. Tant qu'aucun nœud ne répond `primary` (élection Patroni ~30 s, cluster sans quorum), chaque appel refait un balayage complet (rien ne peut être envoyé de toute façon).
- **Ordre par site** (`/etc/alarm-gateway.env`, édition manuelle optionnelle, cf. `docs/REPLACE_ONSITE_NODE.md`) : Izeron `.3, .2, .1, .4, .5, .6, .7` ; Beaufort `.4, .5, .1, .2, .3, .6, .7` (son site d'abord, OVH ensuite, l'autre site, puis alarm-3/alarm-4 tant qu'ils ne sont pas installés, 2026-11-05). Avec le cache, l'ordre ne compte qu'à la première découverte et après une bascule : `install.sh` ne le migre pas.
- **Couverture** : Unit `tests/unit/test_gateway_modem_health.py::TestProbeOrder` : mémorisée en 2e position remontée, déjà en tête, `None`, absente de la liste → ordre inchangé, aucun doublon ; `TestProbeTimeout` : URL mémorisée → `HTTP_TIMEOUT_SECONDS`, autre URL → 3 s, aucune mémorisée → 3 s ; `TestHttpTimeoutDefault` : `HTTP_TIMEOUT_SECONDS` 10 s par défaut, sonde 3 s même avec `HTTP_TIMEOUT_SECONDS=10` dans l'env ; `TestAllNodeUrls` : `NODE6_URL` / `NODE7_URL` en fin de liste, vides ignorés. Le code réseau et les threads ne sont pas testables en unitaire : validation sur les passerelles après déploiement.
### INV-170 [C] ⚠️ Cluster : plus aucun leader vivant vu d'un nœud → email d'alerte depuis ce nœud (2026-10-30)
Chaque backend **qui n'est pas primary** vérifie à chaque tick de `quorum_monitor_loop` (60 s) qu'il voit un **leader vivant** : un backend pair répond à `GET <pair>/health` en 200 avec `role == "primary"`. Les pairs sont ceux du relai d'écriture INV-044 (`PEER_TEST_URLS`, `replica_proxy._PEER_URLS`), sondés en parallèle avec un délai court (2 s) : un leader vivant vu d'ici, c'est exactement « une écriture relayée depuis ce nœud peut aboutir ». Ce `role` vient de `leader_election_loop`, qui lit le `/primary` du Patroni local toutes les 5 s : c'est un signal vivant. La sonde ne dépend ni de l'hôte ni du port des membres Patroni (cluster dev 3 nœuds sur un seul hôte : tous les membres ont l'hôte `host.docker.internal`, chaque backend son port). Un pair primary suffit, même si le Patroni local ne répond pas ou ne liste aucun leader. Un nœud lui-même primary (`is_leader`) voit un leader, sans aucune requête. Sans pairs (`PEER_TEST_URLS` vide : nœud seul, SQLite), seul `is_leader` compte. Le `GET {PATRONI_URL}/cluster` du Patroni local (membre de rôle `leader` ; Patroni n'émet jamais `master` dans `/cluster`) sert seulement à nommer la cause.
- **Le rôle lu dans `/cluster` ne suffit pas** : Patroni le sert depuis des lectures etcd sérialisables (cache local). Après perte du quorum etcd, la clé `/leader` n'expire plus et `/cluster` affiche toujours l'ancien leader `role: leader, state: running`, alors qu'il s'est rétrogradé. Reproduit le 2026-10-30 (image `alarm-patroni:stable`, Patroni 4.1.2, etcd 3.5.17 ; 3 etcd + 2 Patroni, 2 etcd arrêtés) : 45 s, 106 s et 167 s après, `/primary` répond 503 (`role: replica`) sur les deux membres, mais `/cluster`, lu sur chacun, montre encore pg1 `leader` / `running`.
- **Trois causes**, distinguées dans le sujet, le corps et l'audit (`cause`) : `patroni_injoignable` (le Patroni local ne répond pas depuis ce nœud : conteneur arrêté, ou nœud coupé des voteurs etcd ; le reste du cluster peut fonctionner), `aucun_leader` (Patroni répond mais ne liste aucun leader), `leader_hors_service` (un leader est listé mais aucun backend pair ne répond comme primaire). Explication dans le corps : « le leader listé par Patroni (<hôte>) ne répond pas comme primaire : son backend est arrêté ou en erreur, ou il a été rétrogradé (clé leader périmée si le quorum etcd est perdu) ».
- **Sans leader vivant vu pendant plus de 3 minutes** (strictement : 3 min 00 ne suffit pas ; même seuil anti-flapping que INV-085) → **un** email à `SystemConfig.alert_email` (si la clé est absente ou la lecture échoue : `direction_technique@charlesmurgat.com`, comme l'alarme sans chaîne), sujet `[Alarme Murgat] Aucun leader vu de <NODE_NAME> (<lieu>) — <cause en clair>` (lieu : `node_locations`, INV-161). Corps en français formulé « vu de <nœud> », sans affirmation sur tout le cluster : la cause, ce qu'elle implique si elle se confirme (aucune écriture : ni création, ni escalade, ni acquittement ; les passerelles ne récupèrent plus SMS ni appels), quoi vérifier. `log_event("cluster_leader_lost")` (`node`, `since`, `cause`), audité si la base accepte l'écriture ; sinon tracé seulement, jamais d'exception.
- **Un email par épisode, au plus un par heure et par nœud** : pas de répétition tant qu'un leader vivant n'est pas revu. Leader revu → épisode réarmé ; nouvelle perte → nouvel email après 3 min, mais jamais moins d'1 h après le précédent email de ce nœud (un leader qui « bat » ne fait pas un email toutes les 5 min) ; si l'épisode dure encore quand l'heure est écoulée, son email part alors. Une interruption courte (leader vivant vu un tick) remet le compteur des 3 min à zéro.
- **État en mémoire, par nœud** : un redémarrage du backend repart de zéro (au pire 3 min + un tick de retard ; le plafond horaire est oublié). Horloge réelle, pas `clock_now()` (`advance-clock` des tests E2E ne doit rien déclencher).
- **etcd plein** (amendé le 2026-11-13, INV-196) : quand l'email part, le nœud lit l'état des voteurs de `ETCD_HOSTS` ; si l'un a l'alarme NOSPACE, le corps le dit (« etcd est plein (alarme NOSPACE sur <voteurs>) ») avec la procédure (`docs/CD_DESIGN.md` §9sexies). Sujet et cause inchangés.
- **Pourquoi** : INV-085 ne tourne que sur le leader ; quand il n'y en a plus (2 nœuds d'Izeron arrêtés → 1 voteur etcd sur 3, Patroni sans mode failsafe rétrograde le leader), personne n'est prévenu. Audit du 2026-10-30 : « Izeron ×2 → aucun leader → prévenu : personne ». Chaque nœud envoie le sien : sans leader, aucun ne peut se coordonner avec les autres.
- **Couverture** : Unit `tests/unit/test_leader_loss.py` (logique pure `backend/app/logic/leader_loss.py`, mutation 100 %) : hôte du membre `leader` (aucun, `master` ignoré, Patroni injoignable) ; leader vivant dès qu'un pair est `primary` (même Patroni injoignable ou sans leader listé), sinon les trois causes ; 2 min 59 et 3 min 00 → rien, 3 min 01 → email ; pas de doublon ; retour puis re-perte → nouvel email, pas avant 1 h après le précédent (59 min 59 → rien, 60 min → email) ; interruption courte → compteur remis à zéro. Integration `test_leader_loss_inv170.py` (faux Patroni et faux backends pairs par `urlopen`, `is_leader` effacé, email patché, boucle de fond neutralisée) : leader listé dont le backend dit `secondary` (le cas reproduit) → un email `leader_hors_service`, sujet nommant `NODE_NAME` et son lieu, corps avec l'explication, audit avec la cause ; cluster dev sur un seul hôte (membres `host.docker.internal`, leader derrière le port 8002) → aucun email ; Patroni injoignable → `patroni_injoignable` ; aucun leader listé → `aucun_leader` ; leader vivant → rien ; nœud primary au Patroni muet → aucun email ; destinataire par défaut ; câblage : une itération de `quorum_monitor_loop` sur un replica appelle le contrôle, avec l'horloge réelle même après `clock.advance` d'un jour.
- **⚠️ Non prouvé de bout en bout** : le smoke tier 3 (cluster dev, 2 etcd sur 3 arrêtés → email dans Mailhog sous 4-5 min, `/health.cluster_writable: false`) n'a pas été exécuté ; seule la prémisse (`/cluster` périmé, `/primary` 503) a été reproduite, sur Patroni + etcd seuls.

### INV-171 [H] ⚠️ Un nœud sans leader vivant le dit : `/health.cluster_writable`, SMS passerelle, web (2026-10-30)
`GET /health` expose `cluster_writable` : `true` si ce nœud est primary **ou** s'il voit un leader vivant (INV-170 ; `true` tant qu'aucun tick n'a eu lieu), `false` sinon. `GET /api/cluster` expose le même champ, y compris dans sa réponse 503 quand le Patroni local est injoignable. **Le code HTTP reste 200** : `/health` répond à « ce processus est-il sain ? » (base joignable, boucle d'escalade vivante). Un 503 déclencherait à tort le retour arrière du canary (`alarm-cd-canary`) et le détecteur `peer-health`, sur tous les nœuds à la fois, alors que l'image déployée n'y est pour rien : le problème est Patroni/etcd, et un rollback ne le réglerait pas. `GET /api/system/peer-health` (`etcd_quorum_seen`) n'est pas modifié.
- **Passerelle** (`HealthMonitorThread`, `modem_health.backends_down` et `sms_alert_step`) : « tous les backends KO » = **aucun** `/health` en 200 avec `cluster_writable != false` (champ absent = backend d'avant, compté inscriptible ; 503, injoignable ou corps illisible (non JSON) = KO). SMS d'alerte aux `ALERT_RECIPIENTS` : « ALERTE ALARME MURGAT: Aucun backend ne peut ecrire (pas de leader ou tous injoignables). Intervention requise. », même cooldown qu'avant (`ALERT_COOLDOWN_SECONDS`, strict). Le cooldown n'est réarmé (dernier SMS oublié) qu'après **2 passages consécutifs** avec un backend inscriptible : un backend qui (re)démarre se dit inscriptible jusqu'à son premier contrôle (60 s) ; un passage isolé (déploiement, redémarrage) ne doit pas relancer un SMS à chaque fois. Sans `ALERT_RECIPIENTS` dans `/etc/alarm-gateway.env`, le SMS ne part à personne : à vérifier sur les deux passerelles.
- **Web** : pied de page « · CLUSTER EN LECTURE SEULE » (rouge) quand `/health.cluster_writable === false` au chargement ; onglet Cluster (rafraîchi en direct) : bandeau « AUCUN LEADER (sains/total) » (rouge) quand `/api/cluster.cluster_writable === false`, avant le verdict quorum (les rôles listés par `/cluster` peuvent être périmés, INV-170) ; sur la 503 (Patroni local injoignable), l'onglet affiche « Erreur : … », en rouge lui aussi.
- **Pourquoi** : avant, un replica sans aucun leader répondait `/health` 200 « sain » : la passerelle, qui n'alertait que si tous les `/health` échouaient, n'alertait jamais ; l'interface web n'affichait rien. Audit du 2026-10-30, trou G2.
- **Couverture** : Integration `test_leader_loss_inv170.py` : `/health` 200 et `/api/cluster` avec `cluster_writable` `false` puis `true` au retour d'un leader vivant ; chaque cause → `cluster_writable: false` dans `/health` et `/api/cluster` (503 compris, Patroni injoignable) ; nœud primary au Patroni muet → `true`. Unit `test_gateway_modem_health.py::TestBackendsDown` : un 200 inscriptible suffit ; tous en lecture seule → KO ; 503 / injoignables → KO ; 200 sans le champ → inscriptible. `TestSmsAlertStep` : SMS si KO hors cooldown (strict) ; un seul passage inscriptible ne réarme pas le cooldown ; deux passages consécutifs le réarment ; un KO remet la série à zéro.
- **⚠️ Non prouvé de bout en bout** : la boucle `HealthMonitorThread` (requests, port série) et l'affichage web ne sont pas testés (logique pure, `py_compile` et `node --check` seulement) ; smoke tier 3 non exécuté (cf INV-170).
### INV-180 [C] ✅ CD : le retour arrière charge les secrets avant de relancer les conteneurs (2026-10-30)
`alarm-cd-rollback.sh` (appelé par `alarm-cd-canary.sh` au 3e `/health` 503 consécutif, ou à la main en runbook) re-tague `:stable-prev` → `:stable` sur la cible, puis relance les conteneurs par `docker compose --env-file .env.prod.node<N> -p node<N> up -d` (**sans `pull`, sans `--build`** ; `--no-deps patroni backend` pour les non votants node4 à node7). **Avant ce `up -d`, la commande distante charge `.env.prod.secrets`** (`set -a ; . ./.env.prod.secrets ; set +a`, comme `scripts/start-prod-node.sh`) : `docker-compose.yml` exige `SECRET_KEY` (`${SECRET_KEY:?…}`) et les identifiants SMTP viennent du même fichier. Le `up -d` reçoit donc la `SECRET_KEY` du nœud ; si le fichier manque, la commande échoue avant de toucher aux conteneurs.
- **Pourquoi** : constat sur NODE3 le 2026-10-30 : `docker compose config` dans les conditions du rollback échoue sur « SECRET_KEY missing ». Le retour arrière automatique des déploiements (docs/CD_DESIGN.md §5) ne fonctionnait donc pas : l'image était re-taguée mais les conteneurs n'étaient jamais relancés, le nœud restait sur la version cassée et l'event `rollback success` n'était pas émis.
- **Pourquoi pas `start-prod-node.sh`** : sans `--pull` il reconstruit l'image localement (`up --build`), avec `--pull` il retire `:stable` depuis GHCR et écrase le re-tag local — dans les deux cas l'image relancée ne serait pas l'image rétablie.
- **Couverture** : Unit `tests/unit/test_cd_rollback.py` (vrai script ; faux `ssh` qui exécute la commande distante dans un dépôt factice contenant `.env.prod.secrets`, faux `docker` / `curl`) : node3, node4, node6 et node7 → le `up -d` reçoit la `SECRET_KEY` du fichier, services inchangés (`--no-deps patroni backend` pour node4, node6, node7), ni `pull` ni `--build`. RED sur le script d'origine (`SECRET_KEY` vide).

### INV-181 [H] ✅ Nœud sur site : le DNS survit à la perte du réseau local (2026-11-02)
Chaque nœud sur site garde au moins un **résolveur DNS public** (`1.1.1.1` ou `9.9.9.9`) dans `/etc/resolv.conf`, **après** celui du réseau local, qui reste prioritaire (FAILOVER_4G §2.6).
- `infra/onsite/install.sh` (donc `alarm-host-sync` à chaque commit `:stable`) lance `scripts/dns-fallback.sh` : s'il n'y a aucun résolveur public, il ajoute `1.1.1.1` puis `9.9.9.9` à la suite, dans la limite de **3** résolveurs (glibc ignore les suivants). Déjà un public, ou déjà 3 résolveurs : rien ne change.
- `/etc/resolv.conf` **lien symbolique** (géré par systemd-resolved, NetworkManager…) : laissé tel quel, avec un avertissement.
- `/etc/dhcp/dhclient.conf` présent : `append domain-name-servers 1.1.1.1, 9.9.9.9;` ajouté une fois, sinon un renouvellement DHCP effacerait le secours.
- Coût quand la box est morte : chaque résolution attend le délai du premier résolveur (5 s par défaut) avant le public.
- **Pourquoi** : le 2026-11-02, alarm-1 (node4, Beaufort) a perdu son réseau local et est passé sur sa 4G de secours. Le VPN marchait, mais son seul résolveur était la box morte (`172.16.0.1`) : plus de registre d'images, plus de GitHub, donc plus de déploiement, et plus de serveur SMTP pour les emails d'alerte. onsite-1 et onsite-2 avaient déjà un résolveur public, posé à la main.
- **Limite** : un nœud déjà sans DNS ne reçoit pas ce correctif par `alarm-host-sync`, qui a besoin de GitHub. Il le reçoit au retour de son réseau local, ou par une application à la main.
- **Couverture** : Unit `tests/unit/test_dns_fallback.py` (vrai script sur des fichiers temporaires) : box seule → `1.1.1.1` et `9.9.9.9` ajoutés après elle ; public déjà présent → inchangé ; lien → inchangé ; 3 résolveurs → inchangé ; deux passages → identique ; `dhclient.conf` complété une fois, jamais créé.

### INV-182 [C] ✅ Modem et Arduino branchables sur n'importe quelle machine sur site ; le site est celui de la machine (2026-11-05)
La passerelle (`alarm-sms-gateway`, `modem_gateway.py`) et la pile modem (`alarm-4g-standby`, `alarm-modem-rat`, `alarm-modem-watchdog`, `alarm-wan-failover`) sont **installées, activées et démarrées sur chaque machine sur site** (onsite-1, onsite-2, alarm-1 à alarm-4), dans la même version partout (`alarm-gateway-sync`). Le modem SIM7600 et l'Arduino du contact sec se branchent sur **n'importe laquelle**, à chaud, sans aucun geste : sans matériel, rien ne sort en erreur, rien ne redémarre en boucle, aucune route ne change.
- **`infra/onsite/install.sh`** (donc `alarm-host-sync` à chaque commit `:stable`, INV-141) :
  - crée l'utilisateur système `alarm-gateway` (groupe `dialout`) s'il manque ;
  - installe l'unit versionnée `infra/onsite/systemd/alarm-sms-gateway.service` (`ExecStart=python3 /opt/alarm-gateway/modem_gateway.py`, `Restart=always`, `StartLimitIntervalSec=0`) et retire l'ancien drop-in `alarm-sms-gateway.service.d/override.conf` **s'il est identique** à celui qu'il posait (modifié à la main : conservé et signalé) ;
  - pose le code gateway dans `/opt/alarm-gateway` s'il est absent (premier passage, `--auto` compris ; ensuite c'est `alarm-gateway-sync`) ; installe `python3-serial`, `python3-requests`, `python3-pip` (sans lui `alarm-gateway-sync` ne peut pas suivre un changement de `requirements.txt`) et `libqmi-utils` (secours 4G) s'ils manquent. Quand il pose le code du dépôt, il installe `gateway/requirements.txt` (même commande `pip` qu'`alarm-gateway-sync`) et, en cas de succès, le note comme installé (`/var/lib/alarm/cd/gateway-requirements.txt`) : `alarm-gateway-sync` ne relance `pip` que si `requirements.txt` change vraiment (revue PR #296, F3 : sur une machine neuve, le 1er déploiement d'image lançait `pip`, parfois absent → déploiement bloqué à chaque passage) ;
  - masque ModemManager (`systemctl mask --now ModemManager`, sans effet s'il est déjà masqué ou absent) : un modem branché à chaud ne doit pas être pris par lui (PROVISIONING_ONSITE §2.3) ;
  - crée `/etc/alarm-gateway.env` s'il manque (`root:alarm-gateway`, 640), **avec** `DRY_CONTACT_SOURCE=host` et `DRY_CONTACT_NORMAL_VALUE=0` (contact sec par Arduino, seulement à la création : un fichier existant garde sa source, revue F2), puis y ajoute les valeurs non secrètes absentes : `DRY_CONTACT_ENABLED=true`, `GATEWAY_ID` (nom court de la machine), `NODE1_URL` … `NODE7_URL`. **Jamais** une valeur existante modifiée, **jamais** `GATEWAY_KEY` écrit. `GATEWAY_KEY` ou `GATEWAY_SITE` absent ou vide → avertissement ;
  - active et démarre la passerelle (`enable --now` : une passerelle déjà en marche n'est pas relancée ; en `--auto`, elle n'est relancée que si son fichier d'unit a changé, comme les autres units) ; `pyserial`/`requests` absents et non installables → passerelle **non démarrée** (pas de relance en boucle), avertissement. `alarm-4g-standby` et `alarm-modem-watchdog` : activés et démarrés ; `alarm-modem-rat` : activé (il tourne au boot).
- **Site** = celui de la **machine** : `GATEWAY_SITE` de son `/etc/alarm-gateway.env`, posé à la main au provisionnement (alarm-1, alarm-2 : `beaufort` ; alarm-3, alarm-4, onsite-1, onsite-2 : `izeron`). Aucun lien matériel → site : un modem ou un Arduino déplacé prend le site de sa nouvelle machine (INV-155, INV-156, INV-157).
- ~~**Un seul modem par site à la fois** (règle d'exploitation, pas d'arbitrage)~~ **Révisé le 2026-11-12 (INV-193)** : plusieurs passerelles **avec modem** sur le même site lisent la même file (`/internal/sms|calls/pending?site=`, INV-156) ; chacune réserve un SMS ou un appel sur le serveur avant de l'envoyer, une seule l'obtient. Une passerelle **sans modem** ne lit pas ces files (INV-183).
- **Machine sans modem depuis le démarrage** : le watchdog ne dit rien (INV-184) et la passerelle reste « vivante » par ses rapports de contact sec, Arduino ou `no_sensor` (INV-194 ; INV-172 ne se déclenche pas) : c'est le backend qui signale un **site** sans modem (INV-185).
- **Pile modem sans modem** : `alarm-4g-standby` attend `/dev/cdc-wdm0` (une ligne de journal) au lieu de sortir, ne pose aucune route, et monte le lien 4G dès qu'un modem apparaît ; un modem qui disparaît en marche → sortie, relance systemd, nouvelle attente (une relance par disparition, jamais en boucle). `alarm-modem-rat` (au boot) sans `/dev/ttyUSB*` → rien à faire, succès (plus d'unit `failed`). `alarm-wan-failover` ne fait rien sans route 4G (INV-140, inchangé). Watchdog : INV-184.
- **Pourquoi** : décision du propriétaire du 2026-11-05. Faits : sur alarm-1 sans modem, la passerelle sortait (« Modem non detecte. Abandon. ») toutes les 10 s jusqu'à `failed` (`StartLimitBurst` 5 en 60 s) ; l'Arduino d'une machine sans modem n'était jamais lu ; l'unit gateway n'était pas dans le dépôt (posée à la main) et `install.sh` sautait la passerelle là où l'utilisateur `alarm-gateway` manquait.
- **Couverture** : Unit `test_4g_standby.py::test_no_modem_waits_then_brings_up_a_hot_plugged_modem` (vrai script : modem absent pendant 34 attentes → aucune sortie, une seule ligne « en attente », lien monté quand il apparaît) ; `test_modem_set_rat.py` (sans `/dev/ttyUSB*` → code 0). CI `cd-install-smoke.yml`, job `onsite-install` : vrai `install.sh` sur un runner sans modem ni Arduino (utilisateur dans `dialout`, unit du dépôt, drop-in d'origine retiré, `.env` créé 640 sans `GATEWAY_KEY` avec avertissements, valeurs existantes intactes, passerelle `active` sans aucune relance après 60 s, « Gateway operationnelle », 2e passage `--auto` sans relance de la passerelle, pile modem `active` ; revue F2/F3/F6 : `.env` existant sans source → aucune source ajoutée, `pip` présent, `gateway-requirements.txt` noté, puis vrai `alarm-gateway-sync` avec une nouvelle image → déployé **sans** `pip`, passerelle de nouveau « operationnelle » ; ModemManager masqué). Validation matérielle (modem et Arduino déplacés d'une machine à l'autre) : à faire après déploiement.

### INV-183 [C] ✅ Passerelle : modem absent → elle l'attend sans sortir ; le contact sec est lu sans modem (2026-11-05)
- **Contact sec d'abord** : avec `DRY_CONTACT_SOURCE=host`, `HostDryContactMonitorThread` démarre **avant** la recherche du modem et tourne que le modem soit là ou non. Arduino absent ou débranché (aucun port trouvé, ou port configuré impossible à ouvrir) : **un** avertissement, nouvelle recherche toutes les 5 s, reprise seule au rebranchement (un message au retour) ; un nouveau débranchement redonne un avertissement. Sans état lisible, elle rapporte `no_sensor` (INV-194). Le mode `modem` (legacy, GPIO du SIM7600) démarre toujours avec le modem.
- **Recherche du modem** (`modem_health.modem_search_step`), au démarrage puis toutes les **15 s** (`MODEM_RESCAN_SECONDS`) :
  - aucun port série USB SIMCom (VID `1e0e`/`2c7c`) → modem **absent** → attente ; port imposé (`MODEM_AT_PORT` ou `--port`, ex. lien udev ou adaptateur) : **pas** de filtre VID, il est essayé dès qu'il existe (un nom sans `/`, `COM7`, est toujours essayé) ;
  - un port répond `OK` à `AT` → démarrage normal (init, voix INV-132, contrôle INV-169, threads SMS/appels/réception/alerte, keepalive) ;
  - ports SIMCom mais aucune réponse → modem **muet** : attente la 1re fois (modem qui démarre encore), sortie (code 1, INV-126) à la **2e recherche muette de suite** : relance systemd, comptée par le watchdog (INV-146).
- **En attente** : un seul avertissement « en attente du modem », puis `Gateway operationnelle — en attente du modem` (repère attendu par `alarm-gateway-sync` après chaque déploiement). **Aucune** lecture de `/internal/sms|calls/pending`, aucun SMS, aucun appel, pas de SMS d'alerte backends (INV-171) : les SMS et appels du site restent au modem du site (INV-156, INV-145).
- **Modem perdu en marche** (keepalive, INV-126) ou redémarré (INV-169) : sortie inchangée ; relancée 10 s plus tard, la passerelle attend le modem s'il est absent (une relance par perte, jamais en boucle). Le contact sec s'interrompt alors au plus ~20 s (dernier rapport ≤ 5 s avant la sortie + `RestartSec` 10 s + démarrage + premier rapport ≤ 5 s) : le backend ignore une passerelle muette plus de 15 s **sans rien créer ni résoudre** (INV-120, INV-122) et un contact ouvert entre-temps est rapporté au premier envoi.
- **Jamais `failed`** : unit `StartLimitIntervalSec=0`, systemd relance la passerelle quelle que soit la fréquence des sorties.
- **Clé absente** (revue F5) : `GATEWAY_KEY` vide ou valeur par défaut `changeme-in-prod` → **une** ligne ERROR au démarrage (le backend refusera tout en 401). Une même réponse HTTP en erreur (méthode, chemin, code : `POST /internal/alarms/report-state → HTTP 401` toutes les 5 s, par exemple) n'est journalisée qu'**une fois par 10 min** (`modem_health.repeat_log_due`) : pas d'inondation du journal.
- **Pourquoi** : cf. INV-182 (faits du 2026-11-05).
- **Couverture** : Unit `test_gateway_modem_health.py::TestModemSearchStep` ; `test_gateway_plug_anywhere.py` (vrai `main()` de `modem_gateway.py`, port série, threads et attentes remplacés) : sans modem → ne sort pas, contact sec démarré avant la 1re recherche, aucun thread modem, un seul « en attente », « Gateway operationnelle » ; muet 2 fois → code 1 ; modem branché à la 3e recherche → threads modem démarrés, contact sec non doublé ; Arduino absent, branché puis débranché → un avertissement par absence ; port imposé sans VID SIMCom → utilisé, port imposé absent → attente ; clé par défaut → une ERROR ; 100 réponses 401 → un avertissement, un 2e après 10 min. `TestRepeatLogDue`. RED sur le code d'origine : sortie immédiate (code 1) sans contact sec ; port imposé jamais essayé ; 100 avertissements 401.

### INV-184 [C] ✅ Watchdog modem : n'agit que sur un modem déjà vu sur la machine, une seule récupération PCI par épisode (2026-11-05)
`alarm-modem-watchdog` note la présence du modem (`/dev/cdc-wdm0` + périphérique USB `1e0e`) dans `/run/alarm-modem-seen` (effacé au reboot, conservé si le watchdog redémarre).
- **Jamais vu** depuis le démarrage de la machine : modem absent = machine sans modem → **rien** : un message, ni reset PCI, ni email.
- **Vu puis disparu** (toujours absent après la grâce de 20 s) : **une seule** récupération par épisode (PCI remove/rescan du contrôleur xHCI, relance de la passerelle et de la 4G), suivie d'**un** email (« récupéré » ou « DOWN — intervention requise »), puis **attente sans rien toucher**. L'épisode (`/run/alarm-modem-recovered`, posé **avant** la récupération, donc conservé si le watchdog redémarre) se clôt au retour du modem ; une nouvelle disparition ouvre un nouvel épisode.
- Inchangé : modem présent mais muet → reset USB du seul modem (INV-146).
- **Contrôleur laissé retiré** (revue F4) : au démarrage du watchdog, si `/sys/bus/pci/devices/<MODEM_XHCI>` n'existe pas (watchdog tué entre `remove` et `rescan`), il écrit `1` dans `/sys/bus/pci/rescan` : sinon le modem **et l'Arduino** de la machine resteraient coupés. L'email « DOWN » le dit : la récupération réinitialise tout le contrôleur USB, Arduino compris, et si le contrôleur ne revient pas, le contact sec de la machine est coupé aussi (email INV-172 sous 10 min).
- **Une seule récupération par épisode** : choix du propriétaire (protège l'Arduino du même contrôleur) ; l'alerte d'un site resté sans modem vient du backend (INV-185).
- **Conséquence** : retirer le modem d'une machine pour le brancher ailleurs y déclenche **une** récupération (le bus USB est réinitialisé, Arduino compris : quelques secondes sans contact sec) et **un** email ; reboot ou `sudo rm /run/alarm-modem-seen` pour que la machine l'oublie.
- **Pourquoi** : sur une machine sans modem, le watchdog re-tentait la récupération toutes les ~10 min (« toujours absent — nouvelle tentative de recup silencieuse ») : reset PCI du contrôleur USB, donc aussi de l'Arduino du contact sec, sans fin. Décision du propriétaire du 2026-11-05.
- **Couverture** : Unit `test_modem_watchdog.py::TestAbsentAction` (logique pure `absent_action`) et `::TestMainLoop` (vraie boucle `main()`, attentes et E/S remplacées) : jamais vu → ni récupération ni email ; vu puis absent pendant 10 tours → 1 récupération, 1 email ; retour puis nouvelle absence → 2e épisode ; épisode déjà récupéré avant un redémarrage du watchdog → aucune nouvelle récupération ; `/run/alarm-modem-recovered` écrit pendant l'épisode et effacé au retour du modem (mutants M4/M5 de la revue) ; contrôleur absent au démarrage → `rescan`, présent → rien ; email « DOWN » qui nomme l'Arduino. RED sur le code d'origine.

### INV-185 [C] ✅ Site sans modem depuis 10 min → email et audit, une fois par épisode (2026-11-05)
Le backend du **leader** Patroni, à chaque tick de `quorum_monitor_loop` (60 s, après INV-172, **hors de l'event loop** : `asyncio.to_thread`, sa propre session), lit les derniers états de modem (`SystemConfig modem_status:<gateway_id>`, et l'ancienne clé `modem_status`, posés par `POST /internal/alarms/modem-status` à chaque keepalive d'une passerelle **qui a un modem**, INV-129/158 : champs `site` et `reported_at`). Pour chaque site connu (`SystemConfig sites`, INV-155), le **plus récent** état de ses passerelles compte (un modem déplacé d'une machine à l'autre du même site ne déclenche rien). Un site est **sans modem** quand ce plus récent état a **10 min ou plus** (`SITE_MODEM_SILENCE`), ou quand il n'en a **aucun** depuis le début de l'épisode (premier contrôle qui l'a vu sans modem). Logique pure `backend/app/logic/site_modem.py::decide_site_modem`.
- Pour chaque site qui **devient** sans modem depuis 10 min : email à `SystemConfig.alert_email` `[Alarme Murgat] Site <Site> sans modem depuis N min` (conséquence : **SMS et appels impossibles pour ce site**, pas de secours entre sites, ils attendent un modem, INV-156 ; vérifier le branchement USB du SIM7600 sur la machine du site, `journalctl -u alarm-sms-gateway` « En attente du modem », le `GATEWAY_SITE` de la machine qui le porte ; dernier état reçu ou « aucun ») et `log_event("site_without_modem")`, **audité** (`site`, `since`, `minutes`).
- **Un email par épisode** : épisodes tenus dans `SystemConfig site_modem_alert` (JSON `{site: {since, emailed}}`, créé au premier usage, partagé par les leaders successifs). Un état de modem du site **plus récent que le début de l'épisode** le clôt (réarmement) ; un nouveau silence redonne un email.
- **Horloge réelle** (comme INV-168/170), pas `clock_now()` : `advance-clock` des tests E2E ne doit pas déclencher d'email. En prod les deux sont égales (`reported_at` est posé par `clock_now()`).
- `alert_email` vide → rien d'envoyé (warning), l'épisode et l'audit sont tenus. Un site sans passerelle avec modem est signalé, même si son Arduino rapporte (c'est le cas visé).
- **Pourquoi** : revue de la PR #296 (F1). Depuis INV-182/184, un modem absent dès le démarrage d'une machine n'alerte plus personne : le watchdog ignore un modem jamais vu, et l'Arduino de la même passerelle continue `report-state`, donc INV-172 ne se déclenche jamais. Les SMS et appels du site attendaient alors en silence. Constat du 2026-11-05 : Beaufort n'a pas de modem ; le premier contrôle après déploiement enverra donc un email (vrai positif).
- **Couverture** : Unit `tests/unit/test_site_modem.py` (mutation 100 %) : 9 min 59 → rien, 10 min → signalé ; aucun état depuis le début → signalé 10 min après ; un email par épisode ; état plus récent → réarmé, nouveau silence → signalé ; le plus récent des états d'un site compte ; sites indépendants ; état d'un site inconnu ignoré. Integration `test_site_modem_inv185.py` : vrai `POST /internal/alarms/modem-status` puis vrai contrôle → email (sujet, destinataire, corps), audit, clé persistée, pas de 2e email ; réarmement par un nouvel état ; site sans aucun état → email 10 min après le premier contrôle ; modem déplacé (ancien état périmé, nouvel état frais sur le même site) → rien ; un tick de `quorum_monitor_loop` appelle le contrôle hors de l'event loop, avec l'horloge réelle même après `clock.advance`.

### INV-186 [H] ✅ Machine sur site : conformité au standard contrôlée chaque jour, écarts visibles dans le journal (2026-11-06)
Chaque machine du **plan standard** (`STD_PLAN` de `infra/onsite/standard.env` : alarm-1 à alarm-4) est contrôlée **une fois par jour** par `alarm-node-check` (`scripts/alarm-node-check.sh`), lancé par `alarm-node-check.timer` (06:00, ± 30 min, rattrapé au démarrage si la machine était éteinte). `infra/onsite/install.sh` (donc `alarm-host-sync` à chaque commit `:stable`) installe le script (`/usr/local/bin/alarm-node-check`), les valeurs attendues (`/usr/local/share/alarm/standard.env`) et le timer, activé et démarré. Le standard décrit est `docs/STANDARD_MACHINE.md` ; les valeurs attendues sont dans **un seul fichier**, `infra/onsite/standard.env`.
- **Sortie** : une ligne par contrôle, `OK|KO|INFO <contrôle> <détail>`. Code **1** si au moins un KO, **0** sinon, **2** si lancé hors root ou sans `standard.env`. **Lecture seule** : ne corrige rien.
- **Contrôles** : modèle (DMI) et version du BIOS ; démarrage UEFI, **aucune** entrée de démarrage legacy (BBS), Secure Boot dans l'état attendu, première entrée de `BootOrder` = « debian » ; Debian 13 ; `dpkg --audit` vide ; un résolveur DNS public (INV-181) ; ufw actif ; chrony synchronisé ; watchdog systemd (`RuntimeWatchdogUSec`) ; journal persistant (`/var/log/journal`) ; ModemManager masqué ; Docker actif ; `wg0` à l'adresse WireGuard du plan, poignée de main avec NODE3 de moins de 5 min ; `/etc/alarm-gateway.env` : `GATEWAY_KEY` présente et différente de `changeme-in-prod` (**jamais affichée**), `GATEWAY_SITE` = site du plan, `DRY_CONTACT_SOURCE=host` ; chaque unit de `STD_UNITS` `enabled` ; configuration dynamique Patroni sans `archive_timeout` inutile (INV-188, 2026-11-06) ; sur alarm-1 seulement, copie des sauvegardes de la base reçue depuis moins de 26 h (`sauvegarde_base`, INV-191, 2026-11-09).
- **Journal** : sous systemd (`--journal`), les lignes KO sont au niveau **warning** (`journalctl -u alarm-node-check -p warning`), les autres au niveau info ; un KO laisse l'unit `failed` (`systemctl --failed`).
- **Machine hors plan** (onsite-1 et onsite-2 en retrait, NODE3 cloud) : une ligne INFO, aucun contrôle, code 0.
- **Non vérifiable depuis Linux** (checklist sur place, `docs/STANDARD_MACHINE.md` §3) : AC Recovery, Wake-on-LAN, mot de passe administrateur du BIOS.
- **Pourquoi** : demande du propriétaire du 2026-11-06 (standardiser les machines sur site et les pratiques). Inventaire du jour : 4 OptiPlex 9020M avec 3 versions de BIOS (A04, A08, A19), Secure Boot activé sur une seule ; alarm-2 ne redémarrait pas seule (Boot List Option en Legacy, entrée NVRAM « Windows Boot Manager » orpheline). Ces écarts n'étaient vus qu'en allant les chercher.
- **Couverture** : Unit `tests/unit/test_node_check.py` (vrai script, faux `/sys` et `/etc` sous `ALARM_ROOT`, fausses commandes sur le `PATH`, vrai `standard.env`) : machine conforme → que des OK, code 0, clé de passerelle absente de la sortie ; chaque écart (un par contrôle) → KO sur ce seul contrôle, code 1 ; machine hors plan → INFO, code 0 ; hors root → 2 ; `--journal` → `<4>` devant les KO, `<6>` devant le reste ; `install.sh` pose script, valeurs et timer quotidien, l'unit lance `--journal`. CI `cd-install-smoke.yml` (job `onsite-install`) : `systemd-analyze verify` des deux units, timer activé par le vrai `install.sh`, contrôle sur le runner (hors plan) → code 0.

### INV-187 [C] ✅ CD : le leader Patroni n'est jamais redémarré sans bascule planifiée préalable (2026-11-06)
Avant de redémarrer un nœud (`start-prod-node.sh --pull`, donc `docker compose up -d`), `alarm-cd-canary.sh` lit l'état du cluster (`GET :8008/cluster`, sur la cible, sinon sur un autre des 7 nœuds). Si la cible est le **leader** Patroni, il la fait d'abord **basculer** vers un replica sain (`POST http://<IP WG cible>:8008/switchover` `{"leader": "nodeN", "candidate": "nodeM"}`) et attend qu'**un autre membre soit leader et la cible redevenue replica**, **puis** seulement la redémarre. Un nœud qui n'est pas leader : comportement inchangé, aucune bascule.
- **Candidat** : membre non leader, `state == "streaming"`, sur la **timeline du leader** (même définition de « sain » qu'INV-168 ; timeline absente → seul le `state` compte), sans tag `nofailover`. Parmi eux, dans cet ordre : d'abord un nœud qui **n'est plus à redémarrer dans ce passage** (sinon il faudrait le faire basculer à son tour), puis un **votant etcd**, puis le **plus petit `lag`** (`lag` non numérique = classé en dernier, pas exclu). Si tous les candidats sont encore à redémarrer, on en prend un quand même.
- L'orchestrateur passe au canary `--pending "<nœuds suivants du passage>"` et `--voters "<VOTERS>"` (INV-090, seule liste à changer le jour du déménagement). Lancé à la main, le canary prend `--pending` vide et `--voters "1 2 3"`. Depuis le 2026-11-13 (INV-197), `--pending` compte aussi les nœuds reportés plus tôt dans le passage, et le leader passe avant-dernier du passage : sa bascule va vers un nœud déjà sur la nouvelle image.
- **Après la bascule** (amendé le 2026-11-13, INV-197) : le canary attend un backend primary (60 s) avant l'event `switchover/success` ; sinon seconde bascule, arrêt du passage (exit `2`) et email.
- **Pas de bascule terminée → pas de redémarrage**, le canary sort en `3` (« pas prêt », INV-167 : nœud reporté au prochain passage, sans cooldown ; un votant reporté reporte aussi les votants suivants) :
  - aucun candidat sain → `ERREUR : nodeN est le leader Patroni et aucun replica sain…`, aucune demande de bascule ;
  - bascule demandée mais pas terminée en `SWITCHOVER_TIMEOUT` (**60 s**, compté depuis la demande, réponse de Patroni journalisée mais seul l'état du cluster fait foi) → `ERREUR : bascule vers nodeM non terminée…` ;
  - état du cluster illisible depuis les 7 nœuds → `ERREUR : état Patroni (GET :8008/cluster) illisible…` (la cible est peut-être le leader).
- **Traces** : chaque étape est journalisée ; `deployment_events` reçoit `kind=switchover` (nouveau kind accepté par `POST /api/deployments/events`, sans email) : `success` avec `{"from", "to"}`, ou `failure` avec `{"reason": "no_candidate"}` / `{"candidate", "reason": "timeout"}`. Puis `canary_start` comme avant. `--dry-run` affiche la bascule sans la faire.
- **Pourquoi** : prod 2026-11-05/06. Chaque étape canary relançait Patroni + backend ; sur le leader c'était une perte brutale : Patroni ne bascule qu'après son TTL (~15-35 s sans écriture), nouvelle timeline à chaque fois (285 → 290 en un jour), et les replicas hors ligne à ce moment revenaient sur l'ancienne timeline avec leurs WAL déjà supprimés (re-clonage, INV-168). Décision du propriétaire : des déploiements « doux ». Répond aux questions Q4.1 / Q4.3 de `docs/CD_DESIGN.md`.
- **Couverture** : Unit `tests/unit/test_cd_canary_switchover.py` (vrai canary, faux `ssh` / `curl` et faux Patroni) : cible leader → `POST /switchover` vers node2 sur l'API de la cible **avant** `start-prod-node.sh`, events `switchover` puis `canary_start` / `canary_promoted` ; cible replica → aucune bascule ; aucun candidat sain (pas `streaming`, ancienne timeline, `nofailover`), bascule non terminée, état illisible → exit 3, jamais `start-prod-node` ni `compose` ; choix du candidat : votant avant non votant même mieux synchronisé, plus petit lag, jamais une ancienne timeline, pas un nœud encore à redémarrer (même au profit d'un non votant), tous à redémarrer → le plus petit lag. RED sur le script d'origine (le leader était redémarré directement, exit 0). `tests/unit/test_cd_orchestrate.py` : `--pending` = nœuds suivants du passage (`7 6 5 4 2 1` pour node3 … vide pour node1), `--voters "1 2 3"`. Intégration `tests/integration/test_deployments_api.py` : `kind=switchover` → 201. **Non vérifié sur un vrai Patroni** (forme de `/cluster`, durée réelle d'une bascule).

### INV-188 [H] ✅ Cluster : aucune bascule de WAL forcée sans archivage réel (`archive_timeout` à 0) (2026-11-06)
La configuration **dynamique** de Patroni (stockée dans etcd, commune à tous les membres : `patronictl show-config`, ou `GET :8008/config` sur n'importe quel membre) a `postgresql.parameters.archive_timeout` à **0** (ou absent) **tant que `archive_command` n'archive rien** : vide, `true`, `/bin/true`, `/usr/bin/true` ou `:`. Un `archive_timeout` non nul n'est admis qu'avec une **vraie** commande d'archivage. `archive_mode: on` avec `archive_command: 'true'` reste toléré : rien n'est archivé, aucun WAL n'est retenu pour l'archivage, aucune bascule forcée.
- **Contrôle** : `alarm-node-check` (INV-186), contrôle `patroni_archive`, chaque jour sur chaque machine du plan. Il lit `GET http://<IP WireGuard du plan>:8008/config` (API Patroni du membre local : le port est lié à `ADVERTISE_HOST`, l'IP WireGuard, **pas** à 127.0.0.1), `curl --max-time 5`. `archive_timeout` non nul (`10`, `10s`, `1min`…) avec une `archive_command` sans effet → **KO** ; sinon **OK**. API injoignable ou réponse illisible (Patroni arrêté, machine pas encore dans le cluster) → **INFO**, jamais KO.
- **Corriger** (reload seulement, aucun redémarrage de PostgreSQL) : `docker exec <projet>-patroni-1 patronictl -c /etc/patroni/patroni.yml edit-config -p archive_timeout=0 --force`, depuis n'importe quel membre. Ne rien changer d'autre (`max_slot_wal_keep_size` 8GB reste). `patroni/patroni.yml` ne sert qu'au bootstrap : sa section `bootstrap.dcs` n'a aucun effet sur un cluster en service.
- **Bootstrap** (`patroni/patroni.yml`, `bootstrap.dcs`, amendé le 2026-11-09) : mêmes `wal_keep_size` (128MB) et `max_slot_wal_keep_size` (8GB) qu'en prod ; pas d'archivage (ni `archive_mode`, ni `archive_timeout`) tant qu'il n'y a pas de sauvegardes réelles. **Écarts assumés (décision du propriétaire, 2026-11-09)** : `ttl` / `loop_wait` / `retry_timeout` = **30 / 10 / 10 en prod** (valeurs par défaut de Patroni, la configuration dynamique ne les fixe pas ; elles respectent la règle de Patroni `ttl ≥ loop_wait + 2 × retry_timeout`) : cluster réparti sur 3 sites, une bascule inutile coûte plus que les ~10 s gagnées ; le bootstrap garde 20 / 8 / 7 pour les bascules rapides du cluster de la CI (8 + 14 = 22 > 20 : hors règle, admis en CI seulement). `use_pg_rewind` : **vrai partout** — bootstrap, et en prod dans la configuration dynamique depuis le 2026-11-09 (`edit-config -s postgresql.use_pg_rewind=true --force`, sans redémarrage, accord du propriétaire). Essai préalable sur un cluster jetable (image de prod, Patroni 4.1.5, 3 membres, délais 30/10/10) : ancien leader tué brutalement après des écritures jamais répliquées → sans rewind il reste `running` sur l'ancienne timeline (`new timeline … forked off … before current recovery point`, lag affiché 0) ; avec rewind, `pg_rewind exit code=0` et `streaming` sur la nouvelle timeline en ~6 s ; activé alors qu'un membre est déjà bloqué, Patroni le rembobine en un tour. Aucun utilisateur ni `pg_hba` à ajouter (superutilisateur `postgres`, ligne `host all all … md5`), `data_checksums` et `wal_log_hints` actifs. Le rewind ne couvre pas un replica seulement en retard dont le leader a recyclé les WAL : le re-clonage automatique (INV-168) reste le filet.
- **Sauvegardes réelles** (pgBackRest ou autre) : projet séparé, à venir (depuis le 2026-11-09, un dump logique quotidien, INV-191, sans archivage des WAL : rien ne change ici). Il posera une vraie `archive_command` et pourra alors choisir un `archive_timeout` non nul ; ce contrôle l'acceptera sans modification.
- **Pourquoi** : incident prod du 2026-11-06. La configuration dynamique (absente du dépôt) contenait `archive_mode: on`, `archive_command: 'true'` (rien n'est archivé) et `archive_timeout: '10'`. PostgreSQL forçait un nouveau segment WAL de 16 Mo toutes les 10 s, même sans écriture : ~100 Mo/min, ~6 Go/h de WAL quasi vides envoyés aux 6 replicas, 4,6 Go de `pg_wal` sur le leader ; les replicas de Beaufort (derrière le relais OVH) décrochaient après la moindre coupure et devaient être re-clonés (INV-168). Corrigé à chaud avec l'accord du propriétaire (`edit-config -p archive_timeout=0 --force`, reload) : de ~100 Mo/min à ~6 Ko/min de WAL.
- **Couverture** : Unit `tests/unit/test_node_check.py` (vrai script, faux `curl`) : configuration de l'incident → KO sur `patroni_archive` seul, code 1 ; `archive_timeout` 0 → OK ; vraie `archive_command` (pgBackRest) avec `archive_timeout` 60 → OK ; `archive_command` vide, `/bin/true` ou `:` avec `archive_timeout` non nul → KO ; API Patroni injoignable ou réponse illisible → INFO, code 0 ; requête vers l'IP WireGuard du plan, port 8008, avec un délai court. RED sur le script d'origine.

### INV-189 [H] ✅ Cluster : nœud injoignable ou qui ne suit plus le leader depuis plus de 10 min → email et audit, une fois par épisode, puis « rétabli » (2026-11-09)
Le backend du **leader** Patroni (`is_leader`), à chaque tick de `quorum_monitor_loop` (60 s, après INV-185, **hors de l'event loop** : `asyncio.to_thread`, sa propre session), lit `GET {PATRONI_URL}/cluster` et contrôle chaque nœud de `node_locations` (INV-161, les 7 nœuds par défaut) et tout autre membre listé par `/cluster`. Logique pure `backend/app/logic/node_down.py::decide_node_down`.
- **Sain** : le leader `running`, ou un membre `streaming` sur la timeline du leader (même règle que le point d'état des cartes de l'onglet Cluster, INV-161 ; le `lag` est ignoré et une `timeline` absente ne compte pas, seul l'état compte, comme INV-168).
- **Pas sain** : absent de `/cluster` alors qu'il est dans `node_locations` (machine éteinte, câble réseau ou WireGuard coupé, Patroni arrêté) ; tout autre état que `streaming` (`stopped`, `starting`, `unknown`, `running` pour un replica…) ; une autre timeline que celle du leader (`running` sur une ancienne timeline, `streaming` sur une autre).
- **Pas de leader `running` dans `/cluster`, ou Patroni local injoignable** → tick sauté, état inchangé (sans leader, c'est INV-170 qui alerte).
- Pas sain **sans interruption pendant plus de 10 min** (`NODE_DOWN_AFTER`, strictement : 10 min 00 ne suffit pas ; compté depuis le premier tick qui l'a vu pas sain) → **un** email à `SystemConfig.alert_email`, sujet `[Alarme Murgat] Nœud injoignable : <machine> · <nœud> (<Lieu>)` (`node_machines` et `node_locations`, INV-161 : « Izeron », « Beaufort », « OVH », « Lieu inconnu »), ex. `[Alarme Murgat] Nœud injoignable : alarm-2 · node5 (Beaufort)`. Corps : l'état vu par le leader (`absent de /cluster`, `état stopped`, `running sur la timeline 289 (leader : 290)`…), depuis quand (UTC) et combien de minutes, **le reste du cluster a toujours un leader** (`<machine> · <nœud>` du leader : les alarmes fonctionnent, mais le cluster a un nœud de moins pour encaisser une autre panne), quoi vérifier. `log_event("node_down")` **audité** (`node`, `machine`, `location`, `problem`, `since`, `minutes`).
- **Un email par épisode** : épisodes tenus dans `SystemConfig node_down_alert` (JSON `{nœud: {since, emailed}}`, créé au premier usage, pas de migration), **partagé par les leaders successifs** : un changement de leader ne renvoie pas l'email et ne remet pas le compteur à zéro. Un seul tick sain clôt l'épisode (réarmement) : un nœud qui « bat » sous les 10 min n'envoie rien.
- **Retour** : un nœud signalé qui redevient sain → un email court `[Alarme Murgat] Nœud rétabli : <machine> · <nœud> (<Lieu>)` (début de l'épisode, durée, leader) et `log_event("node_recovered")` **audité** (`node`, `machine`, `location`, `since`, `minutes`). **Différence voulue avec INV-172**, qui réarme sans rien envoyer : le propriétaire veut savoir quand le nœud est revenu. Un épisode de moins de 10 min (jamais signalé) se clôt sans email.
- **Plusieurs nœuds** : un email par nœud, chacun avec son propre épisode et son propre délai.
- **Nœud « bientôt obsolète »** (`retiring_nodes`, carte « Legacy (Izeron) ») : contrôlé comme les autres jusqu'au déménagement. Le jour où il est débranché, l'ôter de `node_locations` (comme le prévoit INV-161) : absent de la liste et de `/cluster`, il n'est plus contrôlé et son épisode est oublié sans email.
- **Replica bloqué** (INV-168) : re-cloné au bout de 10 min avec son propre email ; s'il ne suit toujours pas le leader au tick suivant, cet email-ci part aussi, puis « rétabli » quand il repasse `streaming`.
- **Horloge réelle** (comme INV-168/170/185), pas `clock_now()` : `advance-clock` des tests E2E ne doit pas déclencher d'email ; les tests passent le `now` du contrôle.
- `alert_email` vide → rien d'envoyé (warning), l'épisode et l'audit sont tenus.
- **Pourquoi** : demande du propriétaire du 2026-11-09, « alerte quand un nœud est injoignable ». Ce jour-là, alarm-2 (node5, Beaufort) a perdu son câble réseau pendant ~45 min et personne n'a été prévenu : seules existaient l'alerte « aucun leader » (INV-170) et la passerelle muette (INV-172) ; un seul membre tombé ne déclenchait rien (INV-168 ignore un membre absent de `/cluster`).
- **Couverture** : Unit `tests/unit/test_node_down.py` (logique pure, mutation 100 %) : 9 min 59 et 10 min 00 → rien, 10 min 01 → email ; pas de doublon dans l'épisode ; retour → « rétabli » et réarmé ; absent, `stopped`, ancienne timeline, `streaming` sur une autre timeline, timeline absente ; deux nœuds, chacun son délai ; nœud qui bat sous 10 min → rien ; pas de leader `running` → état inchangé ; nœud ôté de `node_locations` et absent → oublié. Integration `test_node_down_inv189.py` (faux Patroni par `urlopen`, email patché) : node5 absent → rien à 10 min, 1 email + 1 audit à 11 min, rien au tick suivant ni après un changement de leader, clé persistée ; retour → 1 email « rétabli » + 1 audit, clé vidée ; deux nœuds (absent, ancienne timeline) → un email chacun à son heure ; un tick de `quorum_monitor_loop` appelle le contrôle hors de l'event loop, avec l'horloge réelle même après `clock.advance`. Dans le reste de la suite tier 2, ce contrôle est neutralisé dans le tick de fond (`tests/integration/conftest.py`), comme INV-172/185. **Non vérifié sur un vrai cluster.**

### INV-191 [H] ✅ Base : sauvegarde logique quotidienne vérifiée, copiée hors site, restauration testée chaque mois, échec → email (2026-11-09)
Décision du propriétaire du 2026-11-09. `scripts/alarm-db-backup.sh` (`/usr/local/bin/alarm-db-backup`) tourne sur **NODE3** en `alarm` : units `alarm-db-backup.{service,timer}` et `alarm-db-restore-test.{service,timer}` (`infra/onsite/systemd/`), posées à la main comme l'orchestrateur CD, **pas** par `install.sh`. La restauration à un instant donné (pgBackRest, archivage des WAL) est un projet ultérieur, hors de cet invariant.
- **Chaque jour** (timer 03:00 UTC + jusqu'à 30 min au hasard, rattrapé au démarrage) :
  - **Source** : vue du cluster `GET http://<IP WG>:8008/cluster` (NODE3 d'abord, puis les 6 autres). Un **replica sain** (`state == "streaming"` sur la **timeline du leader**) : node3 de préférence (lu dans son propre conteneur, `-h 127.0.0.1` : rien ne passe par le WAN), sinon le plus petit `lag` ; **aucun replica sain → le leader**. La source est journalisée (`Source : replica node3`, `Source : leader node1 (aucun replica sain)`). Dump du replica en échec → une seconde tentative sur le leader. Aucun leader `running` dans la vue, ou vue illisible depuis les 7 nœuds → échec.
  - `pg_dump -Fc` de `alarm_db` (PostgreSQL 16 du conteneur local `node3-patroni-1`, utilisateur `alarm`, mot de passe `PGPASSWORD` de `/etc/alarm/db-backup.env` transmis par l'environnement, **jamais en argument**) vers un fichier **temporaire**, vérifié par `pg_restore --list` (archive lisible **et** contenant les données de `users`), puis **renommé** (atomique) en `alarm_db-<AAAAMMJJTHHMMSSZ, UTC>-<nœud source>.dump` dans `/var/lib/alarm/db-backup`. `pg_dump` ou vérification en échec → le temporaire est supprimé, **aucun** fichier n'est gardé.
  - **Rétention 14 jours** (horodatage du nom), appliquée **après une sauvegarde réussie seulement** ; la plus récente n'est **jamais** supprimée. Sauvegardes en échec plusieurs jours de suite → rien n'est supprimé (la dernière bonne reste, même vieille de plus de 14 jours).
  - **Copie hors site** sur **alarm-1** (node4, Beaufort) : SSH dans WireGuard (`alarm@10.99.0.4`, options de l'orchestrateur CD : `BatchMode`, `ConnectTimeout=10`), même nom dans `/var/lib/alarm/db-backup` (créé `0700 alarm` par `infra/onsite/install.sh`), écrite en temporaire puis renommée, **même rétention** là-bas. Copie en échec → la sauvegarde locale est gardée, l'échec est signalé. Chaque tour **rattrape** les sauvegardes qu'alarm-1 n'a pas (amendé le 2026-11-12, ci-dessous).
  - **Une sauvegarde déjà en cours** (verrou `flock -n` sur `/var/lib/alarm/db-backup/backup.lock`) → ce tour est **sauté** : journalisé, code 0, pas d'alerte.
- **Fichiers** : `0600`, dossiers `0700`, propriétaire `alarm`, sur NODE3 comme sur alarm-1 (mots de passe hachés, numéros de téléphone). **Non chiffrés** : les deux machines portent déjà la même base en clair (membres du cluster Patroni) ; la clé de déchiffrement devrait être sur NODE3 pour le test mensuel, et la perdre perdrait toutes les sauvegardes. Le transport est chiffré (SSH dans WireGuard).
- **Chaque mois** (le 1er, 05:00 UTC + jusqu'à 30 min, rattrapé au démarrage), **test de restauration** : la sauvegarde la plus récente de NODE3 est restaurée (`pg_restore --exit-on-error --no-owner --no-acl`) dans un conteneur PostgreSQL 16 **jetable** (`postgres:16-alpine`, nom unique `alarm-db-restore-test-<horodatage>-<pid>`, `--network none` : aucun port, aucun réseau) ; `users`, `escalation_config` et `system_config` (remplies au premier démarrage du backend, jamais vides en service) doivent avoir des lignes. Le conteneur est **toujours** supprimé, succès ou échec.
- **Échec → email** : sauvegarde (vue du cluster, dump, vérification, copie, `PGPASSWORD` absent) ou test de restauration en échec → email `[ALARME-MURGAT] … en echec (<machine>)` avec la raison, par `/opt/alarm/send-alert-email.py` (voie SMTP de la supervision disque, secrets `/opt/alarm/.env.prod.*`), puis code 1 (unit `failed`). Voie **indépendante du cluster** : une sauvegarde échoue souvent quand la base va mal, alors qu'un event `deployment_events` exige un leader joignable.
- **Fraîcheur** : `alarm-node-check` (INV-186) sur alarm-1 (`STD_DB_BACKUP_COPY_NODE=node4` de `standard.env`) : contrôle `sauvegarde_base`, **KO** si la copie la plus récente a été reçue il y a plus de **26 h** ou s'il n'y en a aucune. Témoin indépendant de NODE3 : NODE3 arrêté ou timer désactivé n'envoie aucun email, le contrôle du lendemain matin le voit. Les autres machines n'ont pas ce contrôle.
- **Pourquoi** : jusqu'au 2026-11-09 la base n'avait **aucune** sauvegarde, seulement la réplication Patroni, qui recopie aussi en quelques millisecondes sur les 7 membres une suppression, une migration ratée ou une corruption logique. Décision du propriétaire : dump quotidien pris sur un replica, 14 jours sur NODE3 plus une copie sur alarm-1, test de restauration automatique chaque mois.
- **Mailer** (amendé le 2026-11-10) : `/opt/alarm/send-alert-email.py` lit `/opt/alarm/.env.prod.*` **sauf les modèles `*.example`** du dépôt. Incident du 2026-11-10 à l'installation sur NODE3 : `.env.prod.secrets.example`, trié après `.env.prod.secrets`, écrasait `SMTP_USER`/`SMTP_PASS` par ses valeurs factices (« email KO: Connection unexpectedly closed ») ; les alertes disque de NODE3 passaient par le même chemin. Couverture : `tests/unit/test_send_alert_email.py`.
- **Rattrapage de la copie** (amendé le 2026-11-12, demande du propriétaire) : à chaque tour, après une sauvegarde locale réussie et la rétention locale, la copie envoie sur alarm-1 **chaque sauvegarde locale valide qu'alarm-1 n'a pas**, de la **plus ancienne** à la plus récente, dans la fenêtre de rétention locale (14 jours : une sauvegarde plus vieille n'est jamais envoyée). Même transfert (temporaire puis renommage) et mêmes droits (fichiers 0600, dossier 0700) qu'avant.
  - La liste d'alarm-1 est lue en **un seul** `ssh … ls` ; la rétention d'alarm-1 est ensuite appliquée comme avant. Une sauvegarde déjà présente sur alarm-1 sous le **même nom** n'est **pas** renvoyée : le nom porte l'horodatage UTC et le nœud, et n'apparaît qu'au renommage atomique.
  - **Journal** : une ligne par sauvegarde copiée (`Copie OK sur alarm-1 : <IP>:<dossier>/<nom>`), puis, s'il y a eu rattrapage, une ligne de synthèse `N sauvegardes rattrapées sur alarm-1` (la sauvegarde du jour n'est pas comptée).
  - **Échec**, comme avant : email `copie de <sauvegardes non copiées> sur alarm-1 (…) en echec`, sauvegardes locales gardées, code 1. Une copie en échec arrête le tour : les copies déjà faites restent sur alarm-1, l'email nomme celle qui a échoué et les suivantes, le tour du lendemain les reprend. Liste d'alarm-1 illisible → même échec, **rien** n'est copié, l'email nomme la sauvegarde du jour.
  - Rien ne change pour le dump, la vérification, la rétention locale, le test de restauration ni le verrou.
  - **Pourquoi** : incident du 2026-11-12. alarm-1 est resté éteint 2 jours ; les copies du 11-11 et du 11-12 ont échoué (email correct). Au retour d'alarm-1, seule la sauvegarde de la nuit suivante aurait été copiée : les 2 sauvegardes manquées n'y seraient jamais arrivées, et d'ici là `sauvegarde_base` d'alarm-1 (copie la plus récente de moins de 26 h) restait KO. Le propriétaire les a copiées une fois à la main et a demandé une correction durable.
- **Couverture** : Unit `tests/unit/test_db_backup.py` (vrai script, faux `docker` / `ssh` / `curl` / mailer sur le `PATH`) : node3 replica sain → lu dans son conteneur ; node3 pas `streaming` ou sur une ancienne timeline → replica au plus petit lag, jamais une ancienne timeline ; leader si aucun replica sain ou si le dump du replica échoue ; archive illisible ou `pg_dump` en échec → rien gardé, temporaire supprimé, email ; fichiers 0600, horodatage UTC ; mot de passe jamais en argument ; rétention 14 jours sur NODE3 et alarm-1, rien supprimé après un échec ; copie identique sur alarm-1 par SSH ; copie en échec (SSH coupé, disque plein) → sauvegarde locale gardée, email ; rattrapage (2026-11-12) : alarm-1 sans les 2 précédentes → 3 copies de la plus ancienne à la plus récente, une seule liste, ligne de synthèse ; alarm-1 à jour → seulement la nouvelle ; liste d'alarm-1 illisible → rien copié, email, sauvegardes locales gardées ; copie en échec au milieu → email qui la nomme, copies déjà faites gardées ; sauvegarde hors rétention jamais envoyée ; rétention d'alarm-1 appliquée après le rattrapage ; verrou tenu → rien, code 0 ; vue illisible, sans leader, sans mot de passe → email ; restauration de la plus récente dans un conteneur sans port, toujours supprimé (aussi après un `pg_restore` en échec, une table vide, aucune sauvegarde : email) ; units (utilisateur, horaires). `tests/unit/test_node_check.py` : `sauvegarde_base` OK / KO (vieille de 27 h, absente, copie en cours ignorée) sur alarm-1, absent ailleurs ; même dossier et même machine dans `install.sh`, `standard.env` et le script. Essai réel le 2026-11-09 contre le cluster de dev local (vrais `pg_dump` / `pg_restore` 16, vrai conteneur `postgres:16-alpine` sans réseau, supprimé après succès et après une archive tronquée) et procédure de restauration (`docs/CD_DESIGN.md` §9quinquies) sur un conteneur jetable. **Non vérifié sur NODE3** (replica distant par WireGuard, copie SSH réelle : installation par le propriétaire après fusion).

### INV-192 [M] ✅ Cluster : chaque nœud affiche l'état du modem 4G et du contact sec (Arduino) de sa machine (2026-11-10)
Demande du propriétaire du 2026-11-10 : « Affiche dans la page cluster sur chaque nœud l'état du modem 4G et le status du contact sec/arduino ».
- **API** `GET /api/system/gateways` (**admin** : sans jeton `401`/`403`, non-admin `403`, INV-165) : liste, triée par `gateway_id`, des passerelles qui ont déjà envoyé un état de modem (`SystemConfig modem_status:<gateway_id>`, INV-158) **ou** un état de contact sec (`gateway_states`, INV-120) ; chaque entrée `{gateway_id, modem, dry_contact}`. L'ancienne clé `modem_status` (passerelle sans `gateway_id`) n'est pas listée : elle ne désigne aucune machine.
  - `modem` : `null` si la passerelle n'a jamais envoyé d'état de modem (machine sans modem, INV-183) ; sinon les règles d'INV-129 : `{state, bars, tech, rsrp, updated_at}` (`unknown` au-delà de 180 s sans rapport, `no_network` si non inscrit, sinon `ok` et ses barres).
  - `dry_contact` : `null` si la passerelle n'a jamais rapporté son contact (passerelle d'avant INV-194 sans Arduino) ; sinon `{state, last_seen}`. `state` = l'état du dernier rapport, `closed` (repos), `open` (condition d'alarme) ou `no_sensor` (sans Arduino, INV-194), tant que la passerelle est **vivante** au sens d'INV-122 (dernier rapport de moins de `GATEWAY_LIVENESS_WINDOW_SECONDS`, 15 s par défaut : la même fenêtre que l'agrégation) ; au-delà `silent` quel que soit le dernier état (Arduino débranché ou muet, passerelle arrêtée : le backend ne la compte plus). `last_seen` = `gateway_states.last_seen` (UTC sans fuseau, horloge `clock_now()`) **seulement** quand `silent`, `null` sinon.
  - **Réponse inchangée tant que rien ne change** (INV-135 : le direct ne notifie qu'un changement) : un rapport de contact toutes les 5 s au même état ne la modifie pas. Un état de modem la change à chaque keepalive (60 s), comme `GET /api/system/modem-status`.
- **Web** (onglet Cluster, admin) : deux colonnes dans chaque carte de groupe (INV-161), juste après « Version » : « Modem 4G » (`td[data-label="Modem 4G"]`) et « Contact sec » (`td[data-label="Contact sec"]`). La passerelle d'une ligne est celle dont le `gateway_id` est le nom de sa machine (`machine` de `/api/cluster`, INV-161 ; `GATEWAY_ID` = `hostname -s`, INV-182), **sans tenir compte de la casse**.
  - **Modem 4G** : les 4 barres de l'en-tête (`.modem-signal`, `data-state`, `data-bars`, INV-129) suivies de : `ok` → techno et RSRP (« LTE -95 dBm » ; la techno seule sans RSRP, « WCDMA » ; les barres seules sans l'une ni l'autre, jamais « null ») ; `no_network` → « sans réseau » (barres rouges pulsées) ; `unknown` → « sans nouvelles depuis JJ/MM/AAAA HH:MM » (`updated_at` en heure locale, INV-130). « Sans nouvelles » est le mot de l'en-tête (INV-129) : « modem muet » désigne autre chose dans ce catalogue (présent sur l'USB mais sans réponse, INV-126/146).
  - **Contact sec** : `closed` → badge vert (`badge online`) « fermé » ; `open` → badge rouge (`badge offline`) « ouvert » ; `silent` → badge ambre (`badge waiting`) « muet » (µC muet, INV-120) suivi de « depuis JJ/MM/AAAA HH:MM » (`last_seen` en heure locale) ; `no_sensor` → badge neutre « sans Arduino » (INV-194).
  - `modem` (resp. `dry_contact`) `null`, ou aucune passerelle pour cette machine (OVH, machine sans matériel) → « - ». Chaque cellule porte `data-state` : l'état affiché, ou `none` pour « - ». Textes échappés (`escHtml`, INV-161 : `tech` est un texte libre venu du modem).
  - Les cartes s'affichent **sans attendre** `/api/system/gateways` : « … » jusqu'à sa **première** réponse. Ensuite, chaque nouveau dessin des cartes reprend la dernière réponse, sans « … », puis la redemande : en prod, `/api/cluster` change à chaque avancée du WAL (`lsn` de chaque replica, Patroni 4) et les cartes sont redessinées toutes les quelques secondes. Réponse en erreur → « ? » (pas de `data-state`), y compris aux dessins suivants, tant qu'aucune réponse valide n'est revenue ; la dernière réponse est oubliée.
  - **En direct** : `/api/system/gateways` est surveillé par le WebSocket (INV-135) ; un changement met à jour ces deux cellules **sans** redessiner les cartes ni relancer la sonde des versions (`/api/resilience`).
  - **Non-admin** (INV-165) : `/api/system/gateways` n'est ni appelé ni surveillé, et la sonde des versions (`/api/resilience`, INV-161) n'est pas appelée non plus, même quand `/api/cluster` répond (les cartes sont dessinées pour tous, onglet masqué). Correctif du même jour : depuis le 2026-11-06, chaque chargement des cartes appelait `/api/resilience` pour un non-admin (`403` dans la console).
  - **Mobile** : `data-label` comme les autres colonnes ; à 375 px, ni la page ni une carte ne défile horizontalement (INV-150, INV-161).
- **Limites** :
  - une machine absente de `/cluster` (Patroni arrêté) n'a pas de ligne, donc pas d'état affiché, même si sa passerelle rapporte encore ; une passerelle dont le `gateway_id` n'est le nom d'aucune machine n'apparaît pas ;
  - **pas de purge** : une passerelle sans nouvelles depuis longtemps garde « … depuis <date> », comme l'indicateur de l'en-tête (INV-158), y compris sur l'ancienne machine d'un modem ou d'un Arduino déplacé (INV-182) : d'où l'année dans la date. (INV-172 : un email à 10 min de silence ; au-delà de 7 jours, la passerelle est ignorée.)
  - états lus dans la base du nœud qui sert la page (INV-135) : vu d'un replica en retard de plus de ~10 s sur le leader (point vert de sa carte : le retard n'est pas compté, INV-161/168), les contacts paraissent « muet » alors que le leader les compte vivants ; vu d'un replica bloqué (point rouge), les modems aussi au-delà de 180 s ;
  - app Android (écran Supervision, INV-ANDROID-011) : hors périmètre, PR séparée si demandée.
- **Pourquoi** : depuis INV-182, le modem et l'Arduino se branchent sur n'importe quelle machine sur site. Le propriétaire veut voir, nœud par nœud, où ils sont branchés et s'ils répondent. Jusqu'ici, l'en-tête ne montrait que le signal des modems, un indicateur par passerelle (INV-158), et l'état du contact sec ne se lisait que dans le journal de la passerelle, ou par l'email INV-172 après 10 min de silence.
- **Couverture** : Integration `tests/integration/test_gateway_hardware_inv192.py` : admin seulement ; modem seul, Arduino seul, les deux, contact ouvert, modem sans réseau, tri, ancienne clé `modem_status` absente ; vivacités **séparées** : un keepalive du modem ne garde pas vivant un Arduino débranché (`silent` à 20 s, modem `ok`), un contact qui rapporte ne garde pas le modem (`ok` à 170 s, `unknown` à 190 s) ; contact resté `open` → `silent` aussi, `last_seen` = celui de la base ; fenêtre lue dans `GATEWAY_LIVENESS_WINDOW_SECONDS` ; réponse identique octet pour octet après 3 rapports au même état, différente quand le contact s'ouvre. Playwright `test_frontend.py::TestClusterTab` (hors CI, `/api/cluster`, `/api/resilience` et `/api/system/gateways` simulés) : en-têtes et ordre des colonnes ; chaque état dans la ligne de sa machine, casse ignorée dans les deux sens (`ALARM-2` → alarm-2, machine `Alarm-3` → alarm-3), 3 barres allumées sur 4, techno seule (3G), barres seules, `tech` échappé, « - » pour OVH, passerelle sans machine ignorée ; heures en Europe/Paris avec l'année ; « … » puis « ? » en erreur, « ? » conservé au dessin suivant ; nouveau dessin des cartes avec la dernière réponse, sans « … » ; changement en direct (WebSocket simulé : `/api/system/gateways` dans les chemins surveillés, puis `{"changed"}`) sans redessiner les cartes ni relancer `/api/resilience` ; 375 px sans défilement horizontal ; `TestRoleBasedUI::test_non_admin_never_requests_cluster_admin_data` (`/api/cluster` simulé : ni `/api/system/gateways` ni `/api/resilience` demandés, rien surveillé, aucun `403` ; RED sur le code d'origine à cause de `/api/resilience`). Chaîne réelle vérifiée le 2026-11-10 sur un backend local (vrais `modem-status` et `report-state` → endpoint → WebSocket → cellules, `/api/cluster` simulé ; contact ouvert affiché en 3,4 s). **Non vérifié sur le cluster de prod** (vrais `GATEWAY_ID`, matériel réel).

### INV-194 [H] ✅ Passerelle sans Arduino : elle se dit vivante (`no_sensor`) ; seule une passerelle qui n'envoie plus rien est muette (2026-11-12)
Demande du propriétaire du 2026-11-12 : plus de fausse alerte « passerelle muette » (INV-172) pour une machine qui n'a simplement pas d'Arduino, mais toujours une alerte quand une passerelle est vraiment arrêtée. Cible : chaque machine alarm-x aura son Arduino et son modem ; pendant l'installation et les déménagements, une machine en est souvent dépourvue.
- **Passerelle** (`HostDryContactMonitorThread`, `DRY_CONTACT_SOURCE=host`, INV-183) : sans état de contact lisible — aucun port µC trouvé, port impossible à ouvrir ou perdu (Arduino absent ou débranché), ou µC muet depuis plus de `DRY_CONTACT_LIVENESS_SECONDS` (Arduino non programmé ou figé) — elle envoie `POST /internal/alarms/report-state` avec `state = "no_sensor"`, **à la même cadence** qu'un état lu : à chaque recherche du µC (5 s) ou à chaque poll (`DRY_CONTACT_POLL_SECONDS`, 5 s), dès le démarrage. Journaux inchangés : un avertissement par absence ou par silence du µC.
- **Une valeur d'état, pas un battement de cœur séparé** : même endpoint, même clé, même `gateway_states.last_seen` (lu par INV-172), même fenêtre de vivacité (lue par l'onglet Cluster, INV-192) ; ni table, ni colonne, ni route, ni boucle nouvelle. `gateway_states.state` est un texte sans contrainte : **pas de migration**. Une cadence plus lente suffirait à INV-172 (10 min), mais l'onglet Cluster afficherait la passerelle « muet » entre deux rapports (fenêtre de 15 s) ; 5 s coûte ce que coûte déjà une machine avec Arduino.
- **Backend** : `report-state` accepte `state` ∈ {`open`, `closed`, `no_sensor`} ; `no_sensor` est rangé comme les autres (`last_seen = clock_now()`, `site`).
- **Vivacité (INV-172)** : tout rapport pose `last_seen`, `no_sensor` compris : une passerelle en marche sans Arduino n'est **pas** muette. Elle l'est quand elle n'envoie **plus rien** depuis 10 min (service arrêté, machine éteinte, réseau, WireGuard), qu'elle ait eu un Arduino ou non : email et audit INV-172 inchangés.
- **Contact sec du site (INV-120, 122, 123, 157)** : `no_sensor` n'est ni ouvert ni fermé ; la passerelle est **hors** de l'ensemble des passerelles vivantes **avec capteur** de son site. Sur les états des passerelles vivantes du site (logique pure `backend/app/logic/dry_contact.py::aggregate_dry_contact`) :
  - **création** ⇔ au moins une `open` (OU fail-to-alarm, inchangé) ;
  - **résolution** de l'alarme contact sec ⇔ au moins une passerelle vivante avec capteur, et toutes `closed`. Seulement des `no_sensor` → ni création ni résolution (fail-to-alarm, comme « aucune passerelle vivante », INV-120) ;
  - **désaccord** (INV-123) ⇔ des `open` **et** des `closed` : `no_sensor` n'en ouvre ni n'en clôt aucun, donc jamais d'email de désaccord.
  Un rapport `no_sensor` passe par la même réconciliation sans pouvoir la changer (elle ne dépend que des passerelles avec capteur) ; réponse `{alarm_active, dissensus}` du même calcul.
- **Onglet Cluster (INV-192)** : `dry_contact.state = "no_sensor"` tant que la passerelle est vivante → badge neutre « sans Arduino » (`data-state="no_sensor"`) ; au-delà de la fenêtre de vivacité, `silent` (« muet depuis … ») comme avant.
- **Versions mélangées** (la passerelle suit l'image `:stable` par `alarm-gateway-sync`, aucun ordre de déploiement imposé) : une passerelle d'avant cet invariant n'envoie rien sans Arduino → muette, email INV-172 comme avant, jusqu'à sa mise à jour. Une passerelle à jour face à un backend d'avant : `no_sensor` refusé (`422`, journalisé une fois par 10 min, INV-183), rien d'écrit → comme avant.
- **Limite** : un Arduino débranché, figé ou absent ne donne plus d'email INV-172 (c'est le but pour une machine sans Arduino). Un **site** dont plus aucune passerelle n'avait de contact sec lisible n'était donc signalé par aucun email, seulement dans l'onglet Cluster. **Tranché le 2026-11-13** : email « site sans contact sec » après 10 min, INV-195.
- **Pourquoi** : incident du 2026-11-12. Le propriétaire a déplacé des Arduinos d'une machine à l'autre ; alarm-4 a reçu l'email « passerelle muette » alors que la machine et la passerelle allaient bien : elle n'avait simplement plus d'Arduino, et la passerelle ne rapportait que l'état d'un Arduino lu. Muettes depuis pour la même raison : alarm-2 (Arduino non programmé, ignoré) et alarm-3. `gateway_states` ce jour-là : alarm-1 et onsite-2 vivantes ; alarm-2, alarm-3 et alarm-4 périmées.
- **Couverture** : Unit `tests/unit/test_dry_contact_aggregate.py` (mutation 100 %) : open + no_sensor → création sans désaccord ; closed + no_sensor → résolution ; open + closed + no_sensor → désaccord ; seulement no_sensor, ou rien → ni création ni résolution. `test_gateway_silence.py::test_gateway_alive_without_sensor_is_judged_on_last_seen_only`. `test_gateway_plug_anywhere.py` (vrai `HostDryContactMonitorThread`) : sans Arduino → `no_sensor` avant chaque attente de 5 s, dès le départ ; µC muet (port ouvert, aucune ligne `DC:`) → `no_sensor` à chaque poll de 5 s, un seul avertissement ; Arduino absent, branché puis débranché → `no_sensor`, `closed`, `no_sensor`. Integration `test_gateway_no_sensor_inv194.py` (horloge injectable) : `no_sensor` à T0 et T0 + 9 min → aucun email à T0 + 11 min, email 10 min après le dernier rapport ; open + no_sensor → alarme créée, aucun désaccord après 6 min, fermeture → résolue, aucun email de désaccord ; no_sensor seul → rien de créé, puis l'alarme d'une passerelle devenue muette n'est pas résolue par la passerelle sans capteur ; `/api/system/gateways` → `no_sensor`, puis `silent` 20 s plus tard. Playwright `test_frontend.py::TestClusterTab::test_gateway_without_arduino_shows_sans_arduino` (hors CI). RED avant le correctif : module de logique absent, `422` sur `no_sensor`, aucun rapport de la passerelle sans Arduino ; avec `no_sensor` seulement accepté, la passerelle sans capteur résolvait l'alarme d'une passerelle devenue muette.

### INV-195 [C] ✅ Site sans contact sec lisible depuis plus de 10 min → email et audit, une fois par épisode, puis « rétabli » (2026-11-13)
Demande du propriétaire du 2026-11-13 (limite laissée ouverte par INV-194). Le backend du **leader** Patroni, à chaque tick de `quorum_monitor_loop` (60 s, après INV-172, **hors de l'event loop** : `asyncio.to_thread`, sa propre session), lit `gateway_states` (INV-120) et contrôle chaque site connu (`SystemConfig sites`, INV-155 ; ligne sans site → `default_site` ; ligne d'un site inconnu ignorée). Logique pure `backend/app/logic/site_dry_contact.py::decide_site_dry_contact`.
- **Contact sec lisible** : au moins une passerelle **vivante** du site (dernier rapport de moins de `GATEWAY_LIVENESS_WINDOW_SECONDS`, 15 s par défaut : la fenêtre de l'agrégation, INV-122/157) rapporte `open` ou `closed`. `no_sensor` (INV-194) et une passerelle muette ne comptent pas : c'est l'ensemble des passerelles vivantes **avec capteur** d'`aggregate_dry_contact` qui est vide.
- **Sites suivis** : seulement ceux qui ont **déjà eu** un contact sec lisible, c'est-à-dire vus lisibles par un contrôle, ou avec une ligne `gateway_states` dont le dernier état est `open` ou `closed`, même périmée (un Arduino a été lu jusqu'à ce que sa passerelle se taise). Un site qui n'a jamais eu d'Arduino lisible (rien que des `no_sensor`, ou aucune passerelle) n'est **jamais** signalé. Pourquoi : pendant l'installation ou un déménagement, un site peut ne pas encore avoir d'Arduino (cible : un par machine, INV-194) ; signaler tous les sites connus enverrait un email au déploiement pour un site qui n'a jamais eu de contact, et pour un site configuré avant son matériel. Un site suivi le reste tant qu'il est dans `sites`.
- **Épisode** : compté depuis le **premier contrôle** qui voit un site suivi sans contact lisible (comme INV-189), pas depuis la dernière lecture : un rapport `no_sensor` remplace l'état précédent de la passerelle, l'heure de la dernière lecture n'est donc pas toujours connue. L'alerte n'est jamais en avance, au plus un tick (60 s) en retard.
- Sans contact lisible **plus de 10 min** (`SITE_DRY_CONTACT_LOST_AFTER`, strictement : 10 min 00 ne suffit pas) → **un** email à `SystemConfig.alert_email`, sujet `[Alarme Murgat] Site <Site> sans contact sec` (site capitalisé). Corps : le site ; depuis quand (début de l'épisode, UTC) et combien de minutes ; chaque passerelle du site et son état, `sans Arduino (no_sensor)` ou `muette depuis <last_seen> UTC`, ou « aucune passerelle connue » ; **le contact sec de ce site ne peut plus déclencher d'alarme** ; quoi vérifier (Arduino branché en USB et programmé sur une machine du site, `journalctl -u alarm-sms-gateway`, colonne « Contact sec » de l'onglet Cluster, INV-192). `log_event("site_dry_contact_lost")` **audité** (`site`, `since`, `minutes`).
- **Un email par épisode** : épisodes tenus dans `SystemConfig site_dry_contact_alert` (JSON `{site: {since, emailed}}` des sites suivis, `since` à `null` pour un site lisible ; créé au premier usage, pas de migration), **partagé par les leaders successifs** : une bascule ne renvoie pas l'email et ne remet pas le compteur à zéro.
- **Retour** : un contact lisible revient → l'épisode est clos (réarmé), un nouveau silence redonne un email. S'il avait été signalé : email `[Alarme Murgat] Site <Site> : contact sec rétabli` (début de l'épisode, durée, passerelles qui lisent de nouveau) et `log_event("site_dry_contact_recovered")` **audité** (`site`, `since`, `minutes`). **Différence voulue avec INV-185**, qui réarme sans rien envoyer : comme pour INV-189, le propriétaire veut savoir quand c'est revenu. Un épisode de 10 min ou moins se clôt sans email.
- **Horloge** : `clock_now()`, comme INV-172, car la vivacité se juge sur `gateway_states.last_seen`, posé par `clock_now()`. Un `advance-clock` des tests E2E peut au plus ouvrir un épisode, qui compte depuis ce contrôle.
- `alert_email` vide → rien d'envoyé (warning), l'épisode et l'audit sont tenus.
- **Avec INV-172** : une passerelle muette a son propre email ; si c'était la dernière du site à lire un Arduino, celui-ci part aussi (le contact sec du site est perdu, quelle qu'en soit la cause).
- **Pourquoi** : depuis INV-194, une passerelle sans Arduino lisible rapporte `no_sensor` et ne déclenche plus INV-172. Un site qui perdait son **dernier** contact sec lisible (Arduino débranché, figé ou non programmé sur chaque machine du site) ne prévenait personne : seul l'onglet Cluster le montrait.
- **Couverture** : Unit `tests/unit/test_site_dry_contact.py` (logique pure, mutation 100 %) : 9 min 59 et 10 min 00 → rien, 10 min 01 → email ; pas de doublon dans l'épisode ; retour → « rétabli » et réarmé, nouveau silence → nouvel email ; épisode de moins de 10 min clos sans email ; `no_sensor` et passerelle muette (y compris exactement 15 s) → sans contact lisible ; une passerelle `open` ou `closed` vivante sur le site → rien ; deux sites indépendants ; site jamais lisible → jamais signalé ; ligne `closed` périmée → site suivi, signalé 10 min après le premier contrôle ; site ôté de `sites` → oublié. Integration `test_site_dry_contact_inv195.py` (vrai `report-state`, horloge injectable) : deux passerelles lisibles, l'une passe `no_sensor`, l'autre se tait → rien à 9 min, 1 email (sujet, destinataire, corps) + 1 audit à 11 min, rien au tick suivant, clé persistée, site sans contact jamais lu absent de la clé ; Arduino rebranché → 1 email « rétabli » + 1 audit, épisode clos ; un tick de `quorum_monitor_loop` appelle le contrôle hors de l'event loop, avec `clock_now()`. Dans le reste de la suite tier 2, ce contrôle est neutralisé dans le tick de fond (`tests/integration/conftest.py`, fixture `real_site_dry_contact_check`), comme INV-172/185/189. **Non vérifié sur un vrai cluster.**

### INV-196 [C] ✅ etcd : historique compacté automatiquement ; base d'un voteur à 1 Gio ou plus, ou alarme NOSPACE → email et audit, une fois par épisode (2026-11-13)
Demande du propriétaire du 2026-11-13. etcd ne doit jamais atteindre son quota : à ce point il refuse toute écriture et plus rien n'écrit dans le cluster.
- **Compaction automatique** : le service `etcd` de `docker-compose.yml` fixe `ETCD_AUTO_COMPACTION_MODE=periodic` et `ETCD_AUTO_COMPACTION_RETENTION=1h`, en valeurs **littérales** (aucun `.env` ne peut les changer). C'est le seul service etcd : il vaut pour chaque projet compose qui lance etcd, les voteurs node1 à node3 (`.env.prod.node1..3`), le dev (`.env.dev`), la CI (`.env.node1..3`, `.env.ci-w1/2`) et les futurs voteurs node6/node7 du déménagement (PR #299, même service). etcd compacte toutes les heures en gardant une heure : au plus ~2 h de révisions. Patroni ne lit que la dernière version de ses clés ; une heure suffit pour suivre un incident récent (`etcdctl watch --rev`).
- **La compaction ne défragmente pas** : le fichier garde sa taille ; les pages libérées sont réutilisées, la base cesse donc de grossir. Seule une défragmentation, à la main, réduit le fichier (`docs/CD_DESIGN.md` §9sexies).
- **Appliquer en prod** : etcd ne lit ces variables qu'au démarrage, il faut **recréer** le conteneur etcd de chaque voteur. Le CD ne le garantit pas. Sur un voteur, `start-prod-node.sh --pull` (lancé par le canary) fait `docker compose up -d` de tous les services, ce qui recrée etcd si sa configuration a changé. Mais il ne passe que quand l'image `:stable` change, et il lit le `docker-compose.yml` du checkout `/opt/alarm`, mis à jour à part par `alarm-host-sync` (INV-141) : rien ne garantit que ce checkout est à jour quand le canary passe. Donc recréation **à la main**, CD en pause, **un voteur à la fois**, jamais deux (2 sur 3 pendant la recréation, marge 0, INV-090) : les followers etcd d'abord, le leader etcd en dernier, `endpoint health` et `patronictl list` entre deux. Un voteur dont le conteneur a déjà les variables (`docker inspect`) est sauté (`docs/CD_DESIGN.md` §9sexies).
- **Contrôle** : le backend du **leader** Patroni, à chaque tick de `quorum_monitor_loop` (60 s, après INV-189, **hors de l'event loop** : `asyncio.to_thread`), lit `POST http://<voteur>/v3/maintenance/status` (corps `{}`, délai 3 s) de chaque voteur de `ETCD_HOSTS`, la liste de Patroni, que `docker-compose.yml` passe aussi au backend. `ETCD_HOSTS` vide (tier 2, ou backend pas encore recréé) → contrôle sauté. Réponse illisible ou sans `dbSize` → voteur compté injoignable. Logique pure `backend/app/logic/etcd_space.py::decide_etcd_space`.
  - **Niveau d'un voteur** : `nospace` si l'un de ses `errors` contient `NOSPACE` (alarme active) ; sinon `size` si `dbSize` vaut **1 Gio ou plus** (`ETCD_DB_SIZE_ALERT_BYTES`, 1 073 741 824 octets, la moitié du quota par défaut d'etcd, 2 Gio : `--quota-backend-bytes` n'est pas fixé) ; sinon sain. Le niveau du cluster est le pire des voteurs qui répondent. C'est `dbSize` (taille du fichier) qui compte, pas `dbSizeInUse` : le quota se compare à la taille du fichier.
  - **Un email par épisode et par niveau** : le niveau déjà signalé est tenu dans `SystemConfig etcd_space_alert` (`size`, `nospace`, ou vide ; créé au premier usage, pas de migration, partagé par les leaders successifs). Un email part quand le niveau devient **pire** que celui déjà signalé (rien → `size`, rien ou `size` → `nospace`). Alarme NOSPACE levée (`alarm disarm`) mais base toujours à 1 Gio ou plus → pas d'email, l'épisode redescend à `size` (un nouveau NOSPACE redonne un email). **Réarmé** quand **tous** les voteurs répondent et sont sains (après défragmentation) ; un voteur injoignable (INV-189 le signale) laisse l'épisode tel quel. Pas d'email « rétabli ».
  - **Email** à `SystemConfig.alert_email`, sujet `[Alarme Murgat] etcd : base à <N> Mo sur 2048` (`N` = le plus gros `dbSize` des voteurs, en Mo de 1 048 576 octets) ou `[Alarme Murgat] etcd plein (NOSPACE) : écritures refusées`. Corps : chaque voteur avec sa taille, sa taille utilisée (`dbSizeInUse`) et son alarme, ou « injoignable » ; la conséquence (à 2048 Mo etcd refuse toute écriture : Patroni ne peut plus tenir de leader, plus aucune alarme ne peut être créée, escaladée ni acquittée) ; utilisé ≈ taille → la compaction automatique ne tourne pas, sinon il reste à défragmenter ; la procédure (`docs/CD_DESIGN.md` §9sexies). `log_event("etcd_space_alert")` **audité** (`level`, `db_size` : {voteur : octets, ou `null` si injoignable}).
  - `alert_email` vide → rien d'envoyé (warning), l'épisode et l'audit sont tenus. Aucun délai : pas d'horloge.
- **NOSPACE sans leader** : sous NOSPACE, Patroni ne peut plus écrire dans etcd. S'il perd son leader, ce contrôle (leader seul) ne tourne plus ; c'est l'email INV-170 de chaque nœud qui part. Il ajoute alors, si un voteur de `ETCD_HOSTS` a l'alarme NOSPACE : « etcd est plein (alarme NOSPACE sur <voteurs>) » et la procédure.
- **Pourquoi** : constat en prod du 2026-11-13 (3 voteurs : node1 onsite-1 10.99.0.2, node2 onsite-2 10.99.0.3, node3 NODE3 10.99.0.1 ; etcd 3.5.17, Patroni 4.1.5). Environ 1716 Mo par membre, **tout** utilisé (`dbSizeInUse` = `dbSize`), pour 13 clés seulement mais à la révision 4,69 millions. Aucune compaction n'avait jamais été configurée : etcd gardait chaque révision des clés leader et membres de Patroni, réécrites à chaque boucle. À 2 Go (quota par défaut), etcd lève NOSPACE et refuse les écritures : Patroni perd sa clé leader et plus aucune écriture n'est possible dans le cluster. Personne n'était prévenu avant. Historique compacté et défragmenté à la main le même jour (opération séparée).
- **Couverture** : Unit `tests/unit/test_etcd_space.py` : `docker-compose.yml` lu en YAML (variables de compaction littérales sur le service `etcd`, `ETCD_HOSTS` passé au backend, `ETCD_HOSTS` défini dans chaque `.env` d'un nœud) ; logique pure (mutation 100 %) : 1 Gio − 1 octet → sain, 1 Gio → `size` ; `NOSPACE` → `nospace` même petite ; autre erreur → sain ; un email par épisode ; `size` puis `nospace` → deuxième email ; NOSPACE levé → `size` sans email, nouveau NOSPACE → email ; tous sains → réarmé, nouvelle alerte → email ; voteur injoignable → pas de réarmement, mais un autre voteur au-dessus alerte ; tous injoignables → inchangé. Integration `test_etcd_space_inv196.py` (faux etcd par `urlopen`, email patché) : 3 voteurs à 1716 Mo → 1 email (sujet, destinataire, corps), 1 audit, clé `size`, rien au tick suivant ; NOSPACE sur un voteur → 2e email ; tous défragmentés → réarmé ; `ETCD_HOSTS` vide → aucune requête ; un tick de `quorum_monitor_loop` appelle le contrôle hors de l'event loop. `test_leader_loss_inv170.py` : sans leader, un voteur en NOSPACE → l'email INV-170 le dit. Dans le reste de la suite tier 2, ce contrôle est neutralisé dans le tick de fond (fixture `real_etcd_space_check`). Vérifié sur un etcd 3.5.17 jetable (quota 2 Mo) le 2026-11-13 : forme de `/v3/maintenance/status` (`dbSize` en chaîne, `errors` = `memberID:… alarm:NOSPACE `), journal du démarrage (`auto-compaction-mode periodic`, `auto-compaction-retention 1h0m0s`), procédure à la main (compaction, défragmentation, l'alarme reste jusqu'à `alarm disarm`). **Non vérifié sur le vrai cluster.**

### INV-197 [C] ✅ CD : après chaque bascule planifiée et chaque redémarrage, un backend primary en 60 s ; sinon seconde bascule, arrêt du passage et email (2026-11-13)
Demande du propriétaire du 2026-11-13 (incident du jour, cf. « Pourquoi »). Le CD ne laisse jamais le cluster plus de quelques minutes sans backend qui écrit.
- **Backend primary** : **exactement un** des 7 backends répond `GET http://<IP WG>:8000/health` en 200 avec `role = "primary"` et `cluster_writable = true`. `discover_leader` du canary sonde désormais les 7 nœuds (deux `primary` = aucun) ; le primary trouvé reçoit les `POST /api/deployments/events` suivants. Après une bascule, le backend de l'**ancien** leader ne compte pas : il peut se dire `primary` jusqu'à son tour suivant (5 s).
- **Attente** (`alarm-cd-canary.sh`) : après chaque bascule planifiée terminée (INV-187, avant l'event `switchover/success`) et après chaque redémarrage de la cible (`start-prod-node.sh --pull`, avant le soak), le canary attend un backend primary, sondé toutes les 2 s, au plus `PRIMARY_TIMEOUT` = **60 s**. Pourquoi 60 s : le backend du nouveau leader interroge Patroni toutes les 5 s (`GET :8008/primary`, timeout 2 s, `backend/app/leader_election.py`) et se dit `primary` dès ce tour, **avant** ses migrations INV-152, lancées ensuite hors de l'event loop : ~7 s attendus. Un tour de sondes coûte jusqu'à 21 s quand des nœuds ne répondent pas (3 s × 7). 60 s laisse ~8 fois la normale, reprend la valeur de `SWITCHOVER_TIMEOUT` et borne la coupure due au CD à ~3 min au pire (attente, seconde bascule, seconde attente), au lieu des 18 min du 2026-11-13.
- **Jamais de redémarrage sans primary** : une cible qui n'est pas le leader Patroni n'est redémarrée qu'après la même attente ; sans backend primary → `ERREUR : aucun backend primary en 60s : nodeN pas redemarre`, exit `3` (« pas prêt », rien n'est touché, nœud reporté, INV-167). Une cible leader passe d'abord par sa bascule (INV-187), suivie de l'attente.
- **Aucun backend primary en 60 s** après une bascule ou un redémarrage :
  1. le canary sauve les logs du backend du leader Patroni (cf. **Logs**) puis demande **aussitôt** une seconde bascule planifiée (`POST http://<IP WG du leader>:8008/switchover`) vers un autre replica sain, avec les règles de candidat d'INV-187 (`streaming`, timeline du leader, sans `nofailover` ; d'abord un nœud déjà sur la nouvelle image, puis un votant, puis le plus petit `lag`). Après une bascule, la cible (ancien leader, pas encore redémarrée) compte comme encore sur l'ancienne image ;
  2. il attend de nouveau un backend primary, au plus 60 s ;
  3. **quoi qu'il arrive**, il arrête le passage : exit `2` (canary en échec, INV-167 : l'orchestrateur s'arrête, `failure`, cooldown 1 h), sans redémarrer la cible ni aucun autre nœud. Email par `/opt/alarm/send-alert-email.py` (variable `MAILER`), la voie SMTP de NODE3 qui ne dépend pas d'une base inscriptible (comme INV-191), sujet `[ALARME-MURGAT] CD arrete : aucun backend primary apres la bascule de nodeN vers nodeM` (ou `… apres le redemarrage de nodeN`). Corps : ce qui vient d'être fait, le leader Patroni, la seconde bascule (`nodeM -> nodeK`) et la réponse de Patroni, le résultat (`backend primary retrouve : <URL>` ou `TOUJOURS AUCUN backend primary : intervention requise`), où sont les logs, le passage arrêté et quoi regarder. `post_event` exige un primary : l'event `switchover/failure` `{"reason": "no_primary", …}` (kind sans email backend) n'est envoyé, après l'email, que si la seconde attente a trouvé un primary ;
  4. jamais de boucle : une seule seconde bascule ; état Patroni illisible ou aucun candidat → pas de seconde bascule, même arrêt, même email.
- **Ordre du passage** (`alarm-cd-orchestrate.sh`, amende INV-167/187) : au début d'un passage, l'orchestrateur cherche le backend primary (`discover_leader`, même si `API_BASE` est fixé). Si son nœud est à déployer, il passe **avant-dernier** : le dernier (node1) reste la fin de chaîne (son `canary_promoted` envoie « Déploiement complet ») et, quand vient le tour du leader, les autres nœuds tournent déjà la nouvelle image, sa bascule va donc vers l'un d'eux (un votant de préférence). Ex. node3 leader : `7 6 5 4 2 3 1` au lieu de `3 7 6 5 4 2 1`, une bascule par passage au lieu de deux. Pourquoi l'ordre et pas seulement le choix du candidat : la règle d'INV-187 préfère déjà un nœud qui n'est plus à redémarrer, mais quand le leader passe en premier, tous les autres le sont encore. `--pending` compte aussi les nœuds **reportés** plus tôt dans le passage (injoignables, pas prêts) : ils tournent encore l'ancienne image.
- **Logs** : juste avant de recréer un backend (`start-prod-node.sh`), et pour le leader sans backend primary, le canary sauve `docker logs --timestamps --tail 300 nodeN-backend-1` sur la machine du nœud, dans `/var/lib/alarm/cd/logs/nodeN-<AAAAMMJJTHHMMSSZ>.log` (UTC ; `/var/lib/alarm/cd` est le dossier d'`alarm-cd-pull`, à `alarm`) ; les 10 plus récents sont gardés. Échec → `WARN`, le canary continue. `--dry-run` l'affiche sans le faire. PostgreSQL : `logging_collector` reste éteint, suggestion documentée seulement (`docs/CD_DESIGN.md` §9quater).
- **Pourquoi** : incident du 2026-11-13, ~18 min sans aucune écriture possible en prod (heures UTC, journal d'`alarm-cd-orchestrate` sur NODE3). 07:41:31, passage `3 7 6 5 4 2 1`, node3 leader Patroni. 07:41:41, `[canary-node3] … Successfully switched over to "node1"` (INV-187) : node1 (onsite-1), dernier du passage, tournait encore l'ancienne image. 07:41:50-54, `POST switchover` en échec : aucun backend `role=primary` sur `/health`. Le passage a continué (node3, 7, 6, 5, 4, 2, une étape toutes les ~3 min). De 07:41 à 07:59, Patroni avait un leader (node1, timeline 324) mais aucun backend n'était primary : « Aucun backend primary disponible » toutes les 33 s sur les passerelles, SMS INV-171 « AUCUN BACKEND NE PEUT ECRIRE » ; INV-170 ne s'est pas déclenché (Patroni avait un leader). 07:59:20, `[canary-node1] … switched over to "node2"` : node2, déjà sur la nouvelle image, est devenu primary aussitôt.
- **Limites** : la cause dans le backend de node1 n'est pas prouvée (logs du conteneur perdus à la recréation, `logging_collector` éteint) ; hypothèse : à la promotion, migrations INV-152 ou empilement de verrous, ou `/health` bloqué. La seconde bascule ne répare pas un cluster dont tous les backends sont bloqués : elle borne la coupure due au CD, l'email appelle un humain. Le retour arrière (`alarm-cd-rollback.sh`) et les redémarrages manuels ne sont pas couverts. Orchestrateur et canary sont posés **à la main** sur NODE3 : réinstaller (`docs/CD_DESIGN.md` §9bis). **Non vérifié sur un vrai Patroni.**
- **Couverture** : Unit `tests/unit/test_cd_canary_primary.py` (vrai canary ; faux Patroni qui applique les bascules, backend du leader primary un tour de sondes après sa promotion sauf s'il est bloqué, celui de l'ancien leader encore primary pendant ce tour ; faux `ssh` / `docker` / mailer) : bascule puis primary au tour suivant → étape inchangée, events vers le nouveau primary, aucun email ; nouveau leader jamais primary → ses logs sauvés sur sa machine, seconde bascule vers un nœud déjà à jour (ni l'ancien leader ni un nœud encore à redémarrer), primary retrouvé, exit 2, email puis event `switchover/failure`, jamais `start-prod-node` ; toujours aucun primary → deux bascules seulement, exit 2, email, aucun event ; primary perdu au redémarrage d'un replica → seconde bascule, exit 2, pas de `canary_promoted` ; aucun primary avant de redémarrer un replica → exit 3, rien touché ; logs sauvés avant `start-prod-node.sh`, 10 gardés. `tests/unit/test_cd_orchestrate.py` : leader node3 → `7 6 5 4 2 3 1` (`--pending "1"` pour node3), leader node2, node1 ou inconnu → ordre inchangé ; nœud reporté compté dans le `--pending` des canaries suivants. `tests/unit/test_cd_canary_switchover.py` : faux backend primary = celui du leader Patroni (attentes INV-187 inchangées). RED sur les scripts d'origine : les 8 nouveaux tests en échec (le canary redémarrait sans attendre de primary, exit 0 ; ordre et `--pending` inchangés) ; puis 3 de nouveau en échec quand le faux a gardé l'ancien leader primary un tour (attente satisfaite par cette lecture périmée), d'où l'exclusion de l'ancien leader.

---

## 10. Observabilité et audit

### INV-100 [M] ⚠️ Chaque action critique → AuditEvent
Création, escalade, ack, résolution, modification config, login → entrée dans `audit_events`.
- **Alarmes, toutes sources** (amendé le 2026-11-10) : **chaque** création d'alarme a **exactement un** audit `alarm_created` et chaque résolution un audit `alarm_resolved`, avec son `alarm_id`, **y compris les alarmes des passerelles** (contact sec, `source="gateway_dry_contact"`, INV-120) : `alarm_created` (`source="gateway_dry_contact"`, `assigned_to`) à l'ouverture du contact, `alarm_resolved` (`source="gateway_dry_contact_auto_resolve"`) à la résolution automatique. L'audit est écrit **après le commit** qui rend l'alarme (ou son nouvel état) visible : il passe par sa propre session (`log_event`), et `audit_events.alarm_id` est une clé étrangère vers `alarms.id`. Le `report-state` d'une passerelle ne s'en trouve pas ralenti (moins de 2 s en tier 2).
- **Pourquoi** : requête en lecture sur la prod le 2026-11-10 : **0 des 46** alarmes `source='gateway_dry_contact'` (2026-05-18 → 2026-11-03) avaient leur audit `alarm_created` (46 sur 46 avaient `alarm_resolved`), contre 62 sur 62 pour `source='api'`. L'audit de création était écrit avant le commit de la requête : en PostgreSQL la session d'audit ne voit pas l'alarme pas encore committée, la clé étrangère échoue et `log_event` avale l'erreur. En SQLite (tier 2), l'insert attendait 5 s le verrou d'écriture tenu par la requête (`gateway_states` déjà écrit), puis échouait de même. Même cause et même correctif que l'audit `alarm_escalated` de la boucle (PR #326, 2026-11-09).
- **Couverture** : Integration `test_gateway_audit_inv100.py` : contact ouvert → un `alarm_created` (source, assigné) en moins de 2 s ; contact refermé → un `alarm_resolved` en moins de 2 s. RED avant le correctif (aucun audit, `report-state` à ~5 s). E2E `TestAuditTrail::test_gateway_alarm_audits_created_and_resolved` (PostgreSQL : la clé étrangère de la prod).

### INV-101 [M] ⚠️ correlation_id propagé
Chaque requête a un `X-Correlation-ID` (header), tracé dans les logs et AuditEvent.

### INV-102 [H] ✅ /health retourne 503 si boucle d'escalade stale
Si `last_tick_at < now - 120s` → `/health` renvoie 503 avec `escalation_loop: false`.
- **Pourquoi** : monitoring externe doit détecter le blocage.
- **Fix** : PR0 (a8e1e66). Flag `_simulate_stall` dans `escalation.py` qui bloque la mise à jour de `last_tick_at` pendant le test → plus de race avec le tick concurrent. Endpoint `/test/clear-loop-stall` ajouté pour le teardown propre.
- **Couverture** : E2E `TestSmsAndHealth::test_health_endpoint_returns_503_if_loop_stalled` (5 runs consécutifs passés).

### INV-164 [M] ✅ /health donne la version déployée (2026-10-29)
`/health` (réponse `200` comme `503`) renvoie `commit` = sha git complet de l'image (variable `GIT_SHA`) et `version` = **numéro de release** lu dans `APP_VERSION` (ex. `1.6.1`). Sans `APP_VERSION` : `version` = 7 premiers caractères de `GIT_SHA` ; sans les deux (build local, dev, tests) : `version = commit = "dev"`. L'image backend reçoit les deux au build (`ARG`/`ENV` du `backend/Dockerfile`, `build-args` de `cd-build.yml` : `GIT_SHA` = sha construit, le même que le tag `sha-<court>` ; `APP_VERSION` = contenu du fichier `VERSION` à la racine). Le fichier `VERSION` est bumpé dans la PR de release, avant le tag : l'image construite au merge porte déjà le numéro, alors que le tag n'existe pas encore. La version OpenAPI de FastAPI vaut la même `version`.
- **Pourquoi** : plan du propriétaire du 2026-10-29 : savoir quel commit tourne sur chaque nœud sans `docker inspect`.
- **Lu par l'app Android** pour sa mise à jour intégrée (INV-ANDROID-705) : seule une `version` exactement `X.Y.Z` y déclenche une mise à jour.
- **Couverture** : Integration `test_version_permissions_inv162_164.py` : `test_health_version_dev_without_git_sha`, `test_health_version_from_git_sha` (RED avant le correctif : champs absents), `test_health_version_is_release_number_from_app_version` (RED avant `APP_VERSION` : sha court renvoyé).

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
- `state="no_sensor"` = passerelle vivante sans contact lisible (Arduino absent ou muet) : tient `last_seen`, hors agrégation (INV-194).

Réponse : `{"alarm_active": bool, "dissensus": bool}` (debug gateway).

**Logique backend** (à chaque POST) :
1. **UPSERT** dans table `gateway_states` (`gateway_id` PK, `state`, `last_seen=clock_now()`).
2. **Compute** : `alive_gateways = { g | g.last_seen > clock_now() - liveness_window }` ;
   `should_be_active = any(g.state == "open" for g in alive_gateways)`.
3. **Reconcile** (uniquement sur les alarmes `source = "gateway_dry_contact"`) :
   - `should_be_active=True` ET pas d'alarme `source="gateway_dry_contact"` active
     ET pas d'autre alarme active **du même site** (INV-001, INV-155) → **CREATE** alarme `source="gateway_dry_contact"`,
     effet identique à `POST /api/alarms/send` : assignation pos 1 chaîne via
     `evaluate_alarm_creation_plan`, INV-080 chaîne vide, FCM, audit `alarm_created` après le commit (INV-100),
     `original_created_at = clock_now()` (INV-018).
   - `should_be_active=True` ET autre alarme active du même site (`source != "gateway_dry_contact"`,
     ex: `"api"` ou `"oncall"`) → **no-op** (INV-001 : 1 alarme suffit, le user est
     déjà notifié, on n'écrase pas l'autre source).
   - `should_be_active=False` ET alarme `source="gateway_dry_contact"` active →
     **RESOLVE** (`status="resolved"`, `updated_at=clock_now()`, audit `alarm_resolved` après le commit, INV-100).
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

**Source de lecture du contact** (`DRY_CONTACT_SOURCE`, cf `gateway/config.py`).
Le protocole backend ci-dessus (`report-state`, réconciliation, OR fail-to-alarm)
est **identique quelle que soit la source** — seul le mode de lecture côté gateway
diffère.

- **Mode `host` — RETENU EN PROD (onsite-2, depuis 2026-06-18)** : le contact NC est
  lu par un **microcontrôleur USB (Arduino UNO R4)** branché sur le mini-PC, **pas par
  le modem**. Câblage : contact NC entre une GPIO de l'Arduino et **GND** (`INPUT_PULLUP`,
  zéro composant) → fermé = `DC:0`, ouvert/coupé = `DC:1`. Le firmware
  (`gateway/firmware/dry_contact_r4/`) pousse `DC:<0|1>` sur l'USB-série ;
  `HostDryContactMonitorThread` mappe via `raw_to_state` (`DRY_CONTACT_NORMAL_VALUE=0`)
  et POST le même `report-state`. **Découple le contact-sec du SIM7600** : il reste
  fonctionnel même quand le modem drop du bus USB (récurrent). Détection de vie : µC
  muet > `DRY_CONTACT_LIVENESS_SECONDS` (5 s) → `no_sensor` (INV-194 ; avant le 2026-11-12 : arrêt du report).
  Lu même sans modem, Arduino branchable à chaud sur n'importe quelle machine sur site (INV-182, INV-183).
  Validé bout-en-bout 2026-06-18 (Modbus PLC bit 101 → contact → Arduino → gateway →
  alarme créée + auto-résolution).

- **Mode `modem` — LEGACY (défaut historique, déconseillé)** : pin GPIO 43 du module
  SIM7600 (silkscreen "IO43"), câblage `3V3 ─── [contact NC] ─── IO43`, lecture
  `AT+CGGETV=43`, mapping `value == 1` → `"closed"`, `0` → `"open"`
  (`DRY_CONTACT_NORMAL_VALUE=1`). **Abandonné en prod** : GPIO 1,8 V, pulls imposés, et
  surtout **couplé au circuit d'allumage PWR** du modem (un contact fermé au cold-boot
  empêche le module de démarrer — incidents 2026-06), + contact aveugle quand le modem
  drop. Mode ADC déjà abandonné avant (ADC1 à GND >5 s freeze le firmware modem).

### INV-122 [H] ✅ Redondance hardware multi-gateway, agrégation OR fail-to-alarm
> Multi-site : l'agrégation, le désaccord (INV-123) et la résolution automatique se font **par site**, cf. INV-157.
> Une passerelle `no_sensor` est hors des passerelles alive (ni open ni closed) ; sans passerelle alive avec capteur, no-op, cf. INV-194.

Deux (ou plus) gateways onsite reçoivent le **même contact NC physiquement
splitté** sur leurs entrées respectives (Arduino en mode `host`, ou IO43 du
modem en mode `modem` legacy). Le backend agrège leurs `state`
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
> Multi-site : `alive_gateways` = passerelles vivantes **du même site**, cf. INV-157. Sans les `no_sensor`, cf. INV-194.

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
   - `state TEXT NOT NULL` (valeurs `"open"` ou `"closed"` ; `"no_sensor"` depuis INV-194, sans migration)
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
5. **INV-194 (2026-11-12)** : un Arduino débranché ou figé ne donne plus d'email INV-172. Faut-il une alerte « site sans contact sec depuis 10 min » (aucune passerelle du site avec un capteur lisible), comme INV-185 pour le modem ?

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
