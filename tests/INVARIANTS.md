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

## 1. Alarme — état et cycle de vie

### INV-001 [C] ✅ Une seule alarme active à la fois
À tout instant, il existe **au plus une** alarme avec `status IN ('active', 'escalated')`.
- **Pourquoi** : design explicite (CLAUDE.md). Évite la surcharge cognitive de l'opérateur.
- **Test** : 2 POST /alarms/send en parallèle → exactement 1 réussit (200), l'autre reçoit 409. `COUNT(*) WHERE status IN (active, escalated) <= 1` après toute opération.
- **Exception** : aucune.

### INV-002 [C] ⚠️ Alarme acquittée → suspended_until dans le futur
Si `alarm.status = 'acknowledged'` alors `alarm.suspended_until IS NOT NULL` ET `alarm.suspended_until > now()`.
- **Pourquoi** : l'ACK suspend l'escalade pendant X minutes. Sans cette règle, l'alarme ne peut pas se réactiver.
- **Test** : POST /ack → status=acknowledged ET suspended_until > now. Property : pour toute séquence d'opérations, l'implication tient.

### INV-003 [H] ⚠️ Durée de suspension = 30 min fixe
`alarm.suspended_until = acknowledged_at + 30 minutes` (valeur hardcodée, **NON paramétrable**).
- **Pourquoi** : décision produit (voir IMPROVEMENTS.md #13 REJETE). La durée d'acquittement est gérée par la supervision en amont, le système d'alarme ne fait que relayer avec un délai fixe.
- **Corollaire** : la clé `ack_suspension_minutes` ne doit PAS exister dans `SystemConfig` (supprimée du seed).
- **Test** : POST /ack → suspended_until ≈ now + 1800s (à 10s près).

### INV-004 [C] ⚠️ Alarme active a au moins 1 notifié
Si `alarm.status IN ('active', 'escalated')` alors il existe au moins un `AlarmNotification` lié.
- **Pourquoi** : une alarme sans destinataire n'est pas actionnable.
- **Exception** : chaîne d'escalade vide → email direction technique + alarme persiste avec `assigned_user_id` = fallback user (voir INV-040).

### INV-005 [H] ❌ escalation_count monotone croissant
`alarm.escalation_count` ne peut que croître, jamais décroître, même après ack+réactivation.
- **Pourquoi** : c'est un compteur historique utilisé par les stats.
- **Test** : property-based — après N opérations mixtes, escalation_count[t+1] >= escalation_count[t].

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

### INV-010 [C] ⚠️ Escalade seulement si delay dépassé
`escalation n'a lieu que si (now - alarm.created_at) / 60 >= current_delay`.
- **Pourquoi** : respecter le temps laissé à l'utilisateur pour acquitter.
- **Test** : à delay-1min, pas d'escalade. À delay+1min, escalade.

### INV-011 [C] 🐛 Délai d'escalade uniforme : 15 min pour chaque user
Le délai entre chaque palier d'escalade est de `escalation_delay_minutes` (défaut 15) pour **tous** les users de la chaîne, quelle que soit leur position.
- **Pourquoi** : chaque humain a droit au même temps pour répondre. Pas de "veille" accélérée.
- **🐛 BUG connu** : le code utilise actuellement 2 délais différents (15min position 1, 2min position 2+). La clé `fcm_escalation_delay_minutes` doit être supprimée ou n'être utilisée QUE pour le délai de répétition FCM, pas pour l'escalade.
- **Test** : chaîne [A, B, C], créer alarme → 15min → escalade vers B → 15min → escalade vers C.

### INV-012 [H] ⚠️ Escalade cumulative : les anciens restent notifiés
Après escalade, `notified_user_ids` contient l'ancien assigné ET le nouveau.
- **Pourquoi** : design cumulative (tous sonnent jusqu'à ack).

### INV-013 [C] ⚠️ Escalade wrap around
Après le dernier de la chaîne, l'escalade repart au premier.
- **Pourquoi** : le signal ne doit jamais s'arrêter.
- **Test** : chaîne [A, B, C], alarme assignée à C, escalade → A. Continuer : A → B → C → A (boucle infinie).

### INV-014 [H] ❌ Escalade ignore is_online
L'escalade ne filtre PAS sur `is_online`. FCM réveille le destinataire.
- **Pourquoi** : sinon un user offline bloque la chaîne.
- **Test** : tous users offline → escalade progresse quand même.

### INV-015 [H] ⚠️ Pas d'escalade d'une alarme acknowledged
Une alarme `status = 'acknowledged'` ne peut pas être escaladée (escalation_count ne change pas).
- **Pourquoi** : ACK doit suspendre l'escalade.

### INV-015b [C] 🐛 Tentative de réveil FCM avant escalade si user offline
Avant d'escalader vers le user suivant, **SI** `current_user.is_online == False`, un FCM **high priority** est envoyé au user courant pour tenter de le réveiller, puis l'escalade se fait au même tick. Le user courant reste dans `notified_user_ids` et continuera à recevoir les rappels cumulative.
- **Si le user courant est ONLINE** : pas de FCM dédié — sa sonnerie est déjà active depuis la notification initiale.
- **Pourquoi** : un user offline peut revenir online dans les secondes qui suivent le FCM high-priority → il pourra acquitter avant que le suivant ne soit dérangé plus longtemps.
- **🐛 BUG connu** : le code actuel n'envoie FCM au user courant qu'APRÈS l'escalade (via la boucle cumulative ligne 214). Il n'y a PAS de FCM "wake-up" dédié **conditionné par offline**.
- **Test** : user1 offline → avance 15min → tick → `get_last_fcm_list()` contient une entrée pour user1 au même tick que l'escalade vers user2.
- **Test négatif** : user1 online → avance 15min → tick → PAS de FCM dédié supplémentaire à user1 (seulement le FCM cumulative ligne 214 post-escalade).

### INV-016 [H] ❌ Expiration d'ACK réactive l'alarme
Si `suspended_until < now` et `status = 'acknowledged'` → au prochain tick, `status → 'active'` et `created_at = now`.
- **Pourquoi** : pas de ghost acknowledgment.
- **Test** : ACK → avance 31min → tick → status=active. Boucler l'escalade.

### INV-017 [M] ❌ Reset SMS/Call après réactivation ACK
Après INV-016, toutes les `AlarmNotification.sms_sent` et `.call_sent` sont remises à False pour le cycle suivant.
- **Pourquoi** : la gateway doit re-contacter les users.

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

### INV-019 [M] ❌ Chaîne d'escalade : positions uniques → rejet
Dans `EscalationConfig`, `position` est unique. POST /config/escalation avec position déjà occupée → **rejet** (400 ou 409), pas d'upsert silencieux.
- **Pourquoi** : éviter qu'une mauvaise manip écrase silencieusement un user existant.
- **Test** : POST avec position=1 alors que user1 est à position=1 → 409.

### INV-020 [M] ❌ Chaîne d'escalade : user_id uniques
Un même user ne peut pas être à 2 positions.
- **Pourquoi** : éviter de sonner 2 fois le même user.

---

## 3. Acquittement

### INV-030 [H] ⚠️ Ack enregistre qui et quand
POST /ack → `acknowledged_at = now`, `acknowledged_by = user_id`, `acknowledged_by_name = user.name`.
- **Pourquoi** : audit trail.

### INV-031 [C] 🐛 Seuls les users notifiés peuvent acquitter
POST /alarms/{id}/ack réussit UNIQUEMENT si `current_user.id IN alarm.notified_user_ids`. Sinon → 403.
- **Pourquoi** : éviter les acquittements par erreur (admin qui ne voit pas l'alarme, user qui tape la mauvaise URL, etc.).
- **🐛 BUG connu** : le code actuel (`alarms.py:153-172`) ne vérifie PAS cette condition. Ajouter la vérif.
- **Test** : user3 non-notifié tente ACK sur alarme notifiée à user1,user2 → 403. Ack par user1 → 200.

### INV-032 [M] ⚠️ Ack cumulative : tous les notifiés voient l'alarme acquittée
Après ACK par user1, user2 (qui était aussi notifié) voit l'alarme dans `/mine` avec `status = acknowledged`.
- **Pourquoi** : chacun doit savoir qu'on a répondu.

### INV-033 [L] ⚠️ ack_remaining_seconds décompte correctement
Le champ calculé `ack_remaining_seconds ≈ (suspended_until - now).total_seconds()`.
- **Pourquoi** : UI countdown.
- **Test** : après ack, remaining ≈ 1800. Après +10min, remaining ≈ 1200.

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

### INV-050 [C] ⚠️ Oncall offline > 15min → alarme auto créée
Si user position 1 a `is_online=False` et `last_heartbeat < now - 15min` → création alarme `is_oncall_alarm=True`.
- **Pourquoi** : la continuité de service DOIT être garantie.
- **Test** : avancer horloge, pas de heartbeat position 1 → alarme créée après 15min.

### INV-051 [H] ⚠️ Oncall de retour online → alarme oncall auto-résolue
Si une alarme `is_oncall_alarm=True, status in (active, escalated)` existe et position 1 est `is_online=True` → `status → 'resolved'`.
- **Pourquoi** : le problème s'est résolu de lui-même.

### INV-052 [H] ⚠️ Alarme oncall assignée au SUIVANT, pas au #1
L'alarme oncall n'est PAS assignée à l'user offline (position 1) mais au prochain online dans la chaîne.
- **Pourquoi** : évident (l'offline ne peut pas la prendre).

### INV-053 [C] ⚠️ Personne en ligne → email direction technique
Si tous les users sont offline et position 1 est offline > 15min → email à `system_config.alert_email` (pas d'alarme créée).
- **Pourquoi** : dernier recours, sortir du système.

### INV-054 [H] ⚠️ Pas de doublon d'alarme oncall
Si une alarme `is_oncall_alarm=True, status in (active, escalated)` existe déjà, ne pas en créer une seconde.
- **Pourquoi** : éviter pollution.

### INV-055 [M] ❌ Oncall : seul position 1 déclenche la surveillance
User position 2+ offline ne déclenche PAS d'alarme oncall, même prolongée.
- **Pourquoi** : seul le #1 a un contrat d'astreinte.

---

## 6. SMS et Calls

### INV-060 [H] ⚠️ SMS enqueued après `sms_call_delay_minutes` sur notification
Pour chaque `AlarmNotification`, si `(now - notified_at) / 60 >= sms_call_delay` ET `sms_sent=False` → insert dans `SmsQueue`, set `sms_sent=True`.
- **Pourquoi** : contacter par SMS uniquement si FCM n'a pas réveillé l'user.

### INV-061 [H] ⚠️ Pas de SMS si phone_number NULL
User sans `phone_number` → skip SMS (idem Call).
- **Pourquoi** : pas de destination.

### INV-062 [H] ⚠️ Anti-doublon SMS
Pas de SMS identique (`to_number`, `body`) en état `sent_at=NULL, retries<3` à tout instant.
- **Pourquoi** : éviter spam du user.

### INV-063 [H] ⚠️ Anti-doublon Call
Pas de Call identique (`to_number`, `alarm_id`) en état `called_at=NULL, retries<3`.

### INV-064 [M] ⚠️ SMS/Call exclu après 3 retries
SMS/Call avec `retries >= 3` n'apparaît plus dans `/internal/sms|calls/pending`.
- **Pourquoi** : éviter retry infini sur numéro invalide.

### INV-065 [C] ⚠️ Gateway key obligatoire
`/internal/sms/*` et `/internal/calls/*` nécessitent header `X-Gateway-Key` valide.
- **Pourquoi** : sécurité — ces endpoints exposent les numéros de tel.

### INV-066 [H] 🐛 Toutes les références temporelles utilisent `clock_now()`, pas `datetime.utcnow()`
**Contexte** : le backend a une horloge injectable (`clock.py`) pour que les tests puissent simuler "15 minutes se sont écoulées" en appelant `POST /api/test/advance-clock?minutes=15` au lieu d'attendre réellement 15 minutes. `clock_now()` retourne `datetime.utcnow() + offset`. En prod, l'offset est à 0, donc `clock_now() == datetime.utcnow()`.

**Invariant** : toute écriture de timestamp (`notified_at`, `created_at`, `last_heartbeat`, `acknowledged_at`, `suspended_until`, etc.) doit utiliser `clock_now()`. Sinon, en test, une partie des timestamps est "temps simulé" et une autre est "temps réel" → incohérences.

**🐛 BUG concret** : `alarms.py::_add_notified_user` (ligne 22) crée `AlarmNotification` sans passer `notified_at=clock_now()`. La colonne a un default `datetime.utcnow` (Python, pas injectable). En revanche, `escalation.py:31` passe bien `clock_now()`. Les deux call sites produisent des timestamps différents en test.

**Scénario du bug** :
1. Test : `advance_clock(+16min)` → l'horloge virtuelle est à T+16
2. Création alarme → `AlarmNotification.notified_at = T` (temps RÉEL, car `datetime.utcnow`)
3. Boucle tick : `elapsed = clock_now() - notified_at = (T+16) - T = 16min` → paraît correct par coïncidence
4. MAIS : si on `reset_clock()` puis re-test, `notified_at` est dans le passé réel sans offset → les calculs de délai deviennent imprévisibles

**Test** : `advance_clock(+16)` → POST /alarms/send → requête DB `notified_at` de l'AlarmNotification → assert `notified_at ≈ clock_now()` (à la seconde près), PAS `notified_at ≈ datetime.utcnow()`.

---

## 7. Authentification et sécurité

### INV-070 [C] ⚠️ Login case-insensitive
POST /auth/login avec "ADMIN", "Admin", "admin" → tous acceptés (si password OK).

### INV-071 [M] ⚠️ Nom sans espace
POST /auth/register avec "bad name" → 422 ou 400.
- **Pourquoi** : ergonomie (pas de quoting dans les URLs, etc.).

### INV-072 [M] ⚠️ Nom stocké en lowercase
POST /auth/register avec "TestUser" → stocké "testuser".

### INV-073 [H] ❌ Rate limiting login
Plus de 10 tentatives échouées en 60s pour le même username → 429.
- **Pourquoi** : protection brute-force.
- **Test** : aucun actuellement.

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

### INV-080 [C] ⚠️ Chaîne vide + alarme envoyée → email
POST /alarms/send avec chaîne vide → email direction technique, alarme persiste avec fallback user.
- **Pourquoi** : ne pas perdre l'événement.

### INV-081 [M] ⚠️ /config/escalation/bulk remplace + notifie
POST /config/escalation/bulk → DELETE + INSERT de la chaîne + FCM push à tous les users affectés.

### INV-082 [H] 🐛 /config/escalation/bulk est atomique (transaction unique)
`POST /api/config/escalation/bulk` modifie la chaîne en supprimant TOUTES les règles existantes puis en insérant les nouvelles. Si cette opération n'est pas dans une seule transaction, il existe une **fenêtre de temps (~ms)** pendant laquelle la chaîne est VIDE en base.

**Pourquoi c'est grave** : pendant cette fenêtre, si une alarme arrive (`POST /alarms/send`), le code voit `chaîne vide` → déclenche INV-080 (email direction technique + fallback user). L'alarme est envoyée à la mauvaise personne, et un email inutile est envoyé, juste parce qu'un admin modifiait la chaîne au mauvais moment.

**Invariant** : DELETE + INSERT doivent être dans une transaction SQL unique. Une lecture (GET /config/escalation) concurrente ne doit jamais voir 0 lignes tant que la précédente chaîne était non-vide.

**Test** : thread A fait POST /bulk dans une boucle, thread B fait GET /escalation dans une boucle. B ne doit JAMAIS voir une liste vide (si la chaîne précédente contenait au moins 1 user).

### INV-083 [H] ⚠️ User supprimé → alarmes actives réassignées
DELETE /users/{id} avec alarmes actives assignées → réassignation au premier de la chaîne (hors user supprimé) AVANT delete.
- **Pourquoi** : éviter FK SET NULL orphelin.

### INV-084 [C] 🐛 Tous les délais métier sont paramétrables via SystemConfig
Aucun délai métier ne doit être hardcodé dans le code. Chaque valeur est lue depuis `SystemConfig` à chaque usage (pas cachée), pour qu'un changement admin prenne effet immédiat.

**Inventaire des délais (source de vérité)** :

| Clé SystemConfig | Défaut | Usage | Statut actuel |
|---|---|---|---|
| `escalation_delay_minutes` | 15 | Délai entre chaque palier d'escalade (INV-011) | ✅ lu en DB |
| `sms_call_delay_minutes` | 2 | Délai avant enqueue SMS/Call après notification (INV-060) | ✅ lu en DB |
| `oncall_offline_delay_minutes` | 15 | Délai avant qu'oncall offline déclenche alarme (INV-050) | 🐛 **hardcodé** dans `escalation.py:17` (`ONCALL_OFFLINE_DELAY_MINUTES`) |
| `watchdog_timeout_seconds` | 60 | Délai avant marquer user offline (INV-041) | 🐛 seedé mais **hardcodé** dans `watchdog.py:14` |
| `escalation_tick_seconds` | 10 | Période de la boucle d'escalade | 🐛 **hardcodé** dans `escalation.py:112, 258` |
| `fcm_escalation_delay_minutes` | — | **À SUPPRIMER** : plus de raison d'être depuis INV-011 uniforme | 🐛 encore lu en DB, utilisé pour split astreinte/veille |
| ~~`ack_suspension_minutes`~~ | — | **Non paramétrable** (décision IMPROVEMENTS #13) — 30 min hardcodé voulu | À retirer du seed |

**Pourquoi paramétrer les ticks** (`escalation_tick_seconds`, `watchdog_timeout_seconds`) :
- Non seulement pour la flexibilité admin, mais surtout pour **accélérer les tests**. En test, `escalation_tick_seconds=1` réduit le temps d'attente de 10s à 1s par tick. Gain énorme sur une suite de 66 min.
- En prod, 10s reste la valeur par défaut.

**Règle d'écriture** : toute nouvelle constante temporelle doit être dans SystemConfig dès le jour 1. Pas de "TODO paramétrer plus tard".

### INV-085 [C] 🐛 Perte de quorum cluster → email direction technique
Si le cluster perd son quorum (< majorité de noeuds healthy dans Patroni/etcd), un email est envoyé à `system_config.alert_email` pour alerter la direction technique qu'une intervention manuelle est requise.
- **Pourquoi** : sans quorum, aucun primary ne peut être élu → écritures bloquées → alarmes non traitées. Le système ne peut pas se récupérer tout seul.
- **Conditions de déclenchement** :
  - `quorum.has_quorum == False` dans `/api/cluster` (moins de N/2+1 noeuds healthy)
  - OU Patroni injoignable depuis tous les noeuds pendant > X minutes (seuil à définir, suggestion : 2 min)
- **Anti-spam** : maximum 1 email par heure pour ce cas (éviter le flood si le problème persiste).
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

### INV-102 [H] ⚠️ /health retourne 503 si boucle d'escalade stale
Si `last_tick_at < now - 120s` → `/health` renvoie 503 avec `escalation_loop: false`.
- **Pourquoi** : monitoring externe doit détecter le blocage.
- **🐛 Test flaky** : `test_health_endpoint_returns_503_if_loop_stalled` — revoir le setup.

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

1. **INV-085 (quorum)** : le seuil de détection "Patroni injoignable depuis > X minutes" — je suggère 2 min, confirme ou ajuste.
2. **Suivant** : rédiger `android/INVARIANTS.md` pour les invariants côté client (sonnerie continue, vibration, écran verrouillé, rotation bloquée, reprise post-boot, etc.).
