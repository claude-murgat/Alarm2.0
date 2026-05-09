# Catalogue d'invariants Android — Alarme Murgat

Ce document est la **source de vérité** pour le comportement attendu de l'application
Android Alarme Murgat. Chaque invariant est une règle qui doit être VRAIE à tout moment
(ou après toute opération utilisateur / push / polling).

**Pour l'IA (ou l'humain) qui écrit des tests Espresso** : ne lis PAS le code Kotlin
pour écrire un test. Lis un invariant ici (`INV-ANDROID-XXX`) et écris un test qui
vérifie CETTE règle. Si un invariant te semble ambigu, lève une question dans la PR
description plutôt que d'interpréter depuis le code (le code peut être buggy).

Pour la spec **backend**, voir [tests/INVARIANTS.md](../tests/INVARIANTS.md). Ce
catalogue-ci couvre uniquement la partie client mobile (Kotlin + Espresso).

**Format** : `INV-ANDROID-XXX` stable. Ne jamais renuméroter.
**Criticité** : `C` (critical, alarme manquée / système cassé), `H` (high, bug utilisateur perceptible), `M` (medium, UX dégradée), `L` (low, cosmétique).
**Statut** : ✅ vérifié par tests existants, ⚠️ partiellement couvert, ❌ non testé, 🐛 bug connu / non implémenté.

**Numérotation par section** (espace réservé entre les sections pour ajouter) :
- `001-099` : sonnerie et alarme plein écran
- `100-199` : dashboard et UI principale
- `200-299` : acquittement
- `300-399` : heartbeat et connexion
- `400-499` : failover backend
- `500-599` : auth et session
- `600-699` : FCM et push
- `700-799` : plateforme Android
- `800-899` : tests et isolation

---

## État d'avancement global (2026-04-21, création)

| Catégorie | ✅ | ⚠️ | ❌ | 🐛 |
|---|---|---|---|---|
| 1. Sonnerie / alarme plein écran | 0 | 7 | 0 | 0 |
| 2. Dashboard et UI principale | 0 | 7 | 1 | 0 |
| 3. Acquittement | 0 | 9 | 0 | 0 |
| 4. Heartbeat et connexion | 0 | 5 | 0 | 0 |
| 5. Failover backend | 0 | 5 | 1 | 0 |
| 6. Auth et session | 0 | 8 | 0 | 0 |
| 7. FCM et push | 0 | 4 | 2 | 0 |
| 8. Plateforme Android | 0 | 4 | 0 | 1 (INV-ANDROID-704) |
| 9. Tests et isolation | 0 | 4 | 0 | 0 |

**Total : 58 invariants**, 22 tests Espresso existants (`AlarmE2ETest.kt`) couvrant partiellement 53 invariants (⚠️), 4 non testés (❌), 1 bug connu 🐛 (reprise post-boot non implémentée), 9 questions ouvertes en bas du document.

**Principe à retenir** : aucun invariant n'est encore en ✅ car aucun test n'a été
revu par mutation testing sur l'Android. Les ⚠️ signalent que le test existant
*touche* l'invariant mais n'a pas été renforcé contre des mutations. Avant de
passer à ✅, il faut un test qui casse si on mute la ligne de logique concernée.

---

## 1. Sonnerie et alarme plein écran

### INV-ANDROID-001 [C] ⚠️ AlarmActivity s'affiche en plein écran sur écran verrouillé
Quand le polling détecte une alarme active (`currentAlarm != null`, ID différent du dernier affiché), le service lance `AlarmActivity` via un full-screen intent notification (mécanisme Android 10+). L'activité s'affiche par-dessus l'écran verrouillé.
- **Pourquoi** : l'utilisateur de garde peut être en train de dormir, téléphone verrouillé. Une alarme critique doit apparaître sans nécessiter de déverrouiller.
- **Dépendances plateforme** : permissions `USE_FULL_SCREEN_INTENT` + `POST_NOTIFICATIONS` + manifest `showOnLockScreen="true"` + `turnScreenOn="true"` + `showWhenLocked="true"` sur AlarmActivity.
- **Couverture** : implicite via `test02_activeAlarmShowsOnDashboardWithDuration` (vérifie la carte hero) et `test15_alarmReceivedWhileInBackground` (app en background). Aucun test ne vérifie explicitement le comportement écran verrouillé (nécessite `uiDevice.sleep()` + vérifier que l'activité s'affiche après).
- **Manque** : test Espresso avec `uiDevice.sleep()` + injection d'alarme + vérification que AlarmActivity est au premier plan.

### INV-ANDROID-002 [C] ⚠️ Sonnerie démarre avec AlarmActivity et boucle jusqu'à l'ack
`AlarmSoundManager.startAlarmSound()` lit la ringtone `RingtoneManager.TYPE_ALARM` sur `AudioAttributes.USAGE_ALARM`, `isLooping=true`. La sonnerie ne s'arrête que via `stopAlarmSound()` appelé explicitement après ack ou destroy.
- **Pourquoi** : l'utilisateur peut être à distance du téléphone. Une alarme qui joue 5s puis se tait = ratée.
- **Couverture** : indirect (test03_acknowledgeShowsStatusAndRemainingTime vérifie que l'ack trigger stopAlarmSound via l'absence de sonnerie ensuite — mais l'absence n'est pas vérifiée directement).
- **Manque** : test qui vérifie `soundManager.isPlaying() == true` après alarme affichée.

### INV-ANDROID-003 [C] ⚠️ Volume forcé au maximum sur le stream alarme
À chaque `startAlarmSound()`, `audioManager.setStreamVolume(STREAM_ALARM, maxVol, 0)` est appelé.
- **Pourquoi** : si l'utilisateur a baissé son volume la veille, la sonnerie doit quand même être audible. Le stream ALARM est par design non affecté par le mode silencieux.
- **Manque** : aucun test ne vérifie le volume du stream après déclenchement.

### INV-ANDROID-004 [C] ⚠️ Back button désactivé dans AlarmActivity
`onBackPressed()` overridé vide → le bouton retour ne ferme pas l'activité. L'utilisateur doit acquitter (ou utiliser "Retour au dashboard" qui ne ferme que l'activité, pas l'alarme sous-jacente).
- **Pourquoi** : éviter qu'un geste réflexe fasse manquer une alarme.
- **Manque** : test qui tente `pressBack()` et vérifie que AlarmActivity reste au premier plan.

### INV-ANDROID-005 [H] ⚠️ Pulsation rouge animée pendant alarme active
AlarmActivity anime le background entre `#B71C1C` et `#D32F2F` (1s, reverse infinite) et l'icône scale 1.0 → 1.2 (600ms, reverse infinite).
- **Pourquoi** : signaler visuellement l'urgence en complément de la sonnerie (utilisateur malentendant, téléphone dans la poche).
- **Manque** : aucun test ne vérifie que les animations sont démarrées (difficile à tester, peut être considéré L ou M après debate).

### INV-ANDROID-006 [H] ⚠️ Transition rouge → vert après ack depuis AlarmActivity
Après `acknowledgeAlarm` réussi depuis AlarmActivity : `stopPulseAnimations()`, background anime vers `#1B5E20` (600ms), le bouton ACK + hint est masqué, un bloc confirmation + ArcTimer apparaît (fade-in 400ms).
- **Pourquoi** : feedback utilisateur clair. L'utilisateur doit voir "c'est pris en compte" sans ambiguïté.
- **Couverture** : test03 vérifie l'apparition de `ackStatusText="Acquittée"` (côté dashboard), pas l'animation AlarmActivity.

### INV-ANDROID-007 [C] ⚠️ Sonnerie "connexion perdue" utilise aussi le stream ALARM
Quand `heartbeatLostAlarm` ou `authErrorAlarm` passent true, `connectionLostSoundManager.startAlarmSound()` est démarré (même mécanisme que l'alarme standard).
- **Pourquoi** : une perte de connexion prolongée = même criticité qu'une alarme (on ne recevra rien).
- **Couverture** : test09 vérifie l'affichage du bandeau, pas la sonnerie audio.
- **Manque** : test qui vérifie l'état du MediaPlayer après `heartbeatLostAlarm=true`.

---

## 2. Dashboard et UI principale

### INV-ANDROID-100 [C] ⚠️ Carte hero reflète l'état courant : calm / active / acked
`DashboardActivity.updateStatus()` (1Hz) synchronise la carte avec `AlarmPollingService.currentAlarm` :
- pas d'alarme → `⚪ Tout est calme`, card drawable `card_calm`, dot vert, titre/message/duration/bouton GONE.
- alarme `status=active` → `🔴 Alarme active`, drawable `card_alarm_active` (avec pulsation alpha 1 ↔ 0.85, infinite reverse), dot rouge, bouton ACK VISIBLE, sonnerie démarrée.
- alarme `status=acknowledged` → `🟢 Acquittée` (par nous) ou `Acquittée par X` (autre), drawable `card_alarm_acked`, dot vert, bouton GONE, ArcTimer VISIBLE, sonnerie arrêtée.
- **Pourquoi** : vue d'ensemble immédiate sans scroller / naviguer.
- **Couverture** : test01 (calm), test02 (active), test03/test19 (acked par nous/autre), test08 (retour calm après résolution).

### INV-ANDROID-101 [H] ⚠️ Badge escalade affiche la position si dans la chaîne
Si `escalation_position > 0` en SharedPreferences → badge `"ESCALADE n°N"` avec drawable `badge_escalation`. Sinon `GONE`.
- **Pourquoi** : l'utilisateur doit savoir où il est dans la chaîne pour comprendre quand il sera appelé.
- **Dépendance** : position mise à jour au login + via FCM `type=escalation_update` (voir INV-ANDROID-602).

### INV-ANDROID-102 [H] ⚠️ Label "Actuellement de garde" visible UNIQUEMENT si is_oncall
Label distinct du badge escalade. Affiché si `is_oncall=true` (user position 1 effective). Position 2/3 dans la chaîne → badge visible, label GONE.
- **Pourquoi** : différencier "je suis dans la chaîne" (je serai peut-être appelé) de "je suis le premier appelé maintenant".

### INV-ANDROID-103 [M] ⚠️ Statut système 3 états : Connecté / En attente / Déconnecté
Calcul dans `updateStatus()` :
- `isRunning && lastHeartbeatOk` → `● Connecté au serveur` (vert)
- `!isRunning && !isOncall` → `● En attente` (orange, non-astreinte attendant un push FCM)
- sinon → `● Déconnecté` (rouge)
- **Pourquoi** : comprendre pourquoi on ne reçoit (pas) d'alarme.
- **Manque** : aucun test ne couvre l'état "En attente" spécifique au mode veille.

### INV-ANDROID-104 [H] ⚠️ Alerte "Connexion perdue" : bandeau permanent, pas toast
Quand `heartbeatLostAlarm || authErrorAlarm`, `connectionLostContainer.visibility=VISIBLE` avec texte explicite :
- `heartbeatLostAlarm` → "Connexion perdue avec le serveur", bouton reconnexion GONE (resolution automatique attendue).
- `authErrorAlarm` → le message personnalisé (ex: "Votre session a expiré..."), bouton "Reconnexion" VISIBLE (action manuelle requise).
- **Pourquoi** : un toast qui disparaît en 3s = l'utilisateur ne le voit pas s'il n'a pas le téléphone en main. Un bandeau permanent force la lecture.
- **Couverture** : test09 (heartbeat lost), test17 (auth error).

### INV-ANDROID-105 [M] ⚠️ Historique affiché = 10 derniers jours filtrés sur resolved/acknowledged
`getAlarmHistory(days=10)` appelé toutes les 10s, filtré côté client sur `status in {resolved, acknowledged}`. Affichage en liste chronologique avec timeline (dot + ligne) côté gauche.
- **Pourquoi** : contexte récent pour l'opérateur sans scroller 90 jours.
- **Couverture** : test07 (présence d'une alarme résolue).

### INV-ANDROID-106 [L] ⚠️ Durée "Active depuis X" calculée client-side depuis created_at
`computeDuration(alarm.created_at)` produit `"Nh Mmin"`, `"Mmin"`, ou `"quelques secondes"`. Le parsing assume UTC ISO 8601 et utilise `System.currentTimeMillis()` comme référence.
- **Pourquoi** : lisibilité. Dépend de l'horloge du téléphone (désynchro possible avec backend).
- **Couverture** : test02 vérifie la présence du substring "depuis", pas la valeur calculée.

### INV-ANDROID-107 [M] ❌ Bouton aide flottant exporte les 500 derniers logs via share intent
`helpFab.setOnClickListener { shareLogs() }` → `Intent.ACTION_SEND` MIME `text/plain` avec contenu `AppLogger.exportLogs(userName, version)`. Le contenu inclut un en-tête (user, version, device, Android, timestamp, count) + les 500 dernières entrées au format `[ts] tag: message`.
- **Pourquoi** : debug à distance par l'équipe technique sans accès ADB. Mesure de résilience opérationnelle.
- **Manque** : aucun test ne vérifie la présence du bouton ni l'envoi du share intent.

---

## 3. Acquittement

### INV-ANDROID-200 [C] ⚠️ Bouton ACK visible UNIQUEMENT quand alarme active et user notifié
`dashboardAckButton.visibility=VISIBLE` ssi `alarm != null && alarm.status == "active" && !isAcknowledged`. Le backend filtre `/alarms/mine` sur les utilisateurs notifiés → si l'alarme n'apparaît pas, pas de bouton à afficher.
- **Pourquoi** : pas de bouton trompeur qui mènerait à un 403. L'UI reflète la possibilité réelle d'action.
- **Couverture** : test02 (VISIBLE), test01 (GONE sans alarme), test19 (GONE sur ack par autre user).

### INV-ANDROID-201 [C] ⚠️ ACK côté client = POST /api/alarms/{id}/ack avec le token bearer
`acknowledgeCurrentAlarm()` → `ApiProvider.service.acknowledgeAlarm("Bearer $token", alarm.id)`. La source de vérité est le backend (qui applique INV-031 côté serveur).
- **Couverture** : test03 assert `acknowledgeCalledWith == Pair(bearer, 42)`.

### INV-ANDROID-202 [C] ⚠️ ACK 2xx → sonnerie stop, bouton masqué, ArcTimer visible
Séquence garantie après `response.isSuccessful` :
1. `soundManager.stopAlarmSound()`
2. `isAcknowledged=true`, `acknowledgedAlarmId=alarm.id`
3. UI thread : bouton `GONE`, `ackStatusText="Acquittée"` VISIBLE, `ArcTimerView.setTime(1800, remaining)` VISIBLE.
- **Pourquoi** : feedback immédiat non-ambigu + arrêt de la pollution sonore.
- **Couverture** : test03 (présence de ackStatusText + ackRemainingTime).

### INV-ANDROID-203 [H] ⚠️ ArcTimer : texte central refl ète ack_remaining_seconds / 60
`setTime(max, remaining)` → `centerText = "${remaining/60} min"`. Arc vert proportionnel `360f * remainingSeconds / maxSeconds`, départ à `-90°` (12h).
- **Pourquoi** : feedback temps restant avant ré-activation.
- **Couverture** : test20 vérifie "30 min" puis "29 min" après poll avec `ack_remaining_seconds=1740`.

### INV-ANDROID-204 [M] ⚠️ ArcTimer change de couleur : vert > 30% > orange > 10% > rouge
Calcul `ratio = remainingSeconds / maxSeconds` :
- `ratio > 0.3` → vert `#22c55e`
- `ratio > 0.1` → orange `#f59e0b`
- sinon → rouge `#ef4444`
- **Pourquoi** : urgence visuelle à mesure que l'expiration approche.

### INV-ANDROID-205 [H] ⚠️ Alarme acquittée par un AUTRE user → statut "Acquittée par X", pas de sonnerie
Quand `alarm.status=="acknowledged" && !isAcknowledged` (nous n'avons pas déclenché l'ack) : UI montre `"Acquittée par $acknowledged_by_name"`, carte verte, sonnerie ARRÊTÉE, ArcTimer VISIBLE avec `ack_remaining_seconds`, bouton ACK GONE.
- **Pourquoi** : cumulative → tous les users notifiés voient l'ack. L'app ne doit pas continuer à sonner une alarme prise en charge.
- **Couverture** : test19.

### INV-ANDROID-206 [H] ⚠️ Nouvelle alarme (ID différent) après résolution → reset complet du state ACK
Quand `alarm != null && isAcknowledged && alarm.id != acknowledgedAlarmId` → `isAcknowledged=false`, `alarmGoneDuringAck=false`, `acknowledgedAlarmId=null`. Bouton ACK redevient VISIBLE, sonnerie redémarre, carte rouge.
- **Pourquoi** : chaque alarme est un incident distinct. Ne pas figer l'état "acquitté" entre incidents.
- **Couverture** : test21.

### INV-ANDROID-207 [H] ⚠️ Ré-activation après expiration d'ack → reset ACK local
Si le backend remet l'alarme en `status="active"` après expiry (INV-016 backend) et qu'on a son ID en `acknowledgedAlarmId` → reset du flag `isAcknowledged`. Bouton ACK redevient VISIBLE, sonnerie redémarre.
- **Pourquoi** : écho de INV-016 côté client. L'expiration d'ack côté backend doit re-déclencher l'UX alarme.
- **Couverture** : test11 (3 phases : active → ack → suspension → réactive).

### INV-ANDROID-208 [H] ⚠️ Alarme disparue pendant l'ack → pas de re-sonnerie si elle ne revient pas
Si `alarm == null && isAcknowledged` → `alarmGoneDuringAck=true`, carte passe calm, pas de sonnerie. Si elle revient ensuite avec le même ID, reset pour re-sonner. Sinon, reste calm.
- **Pourquoi** : alarme résolue côté backend pendant notre fenêtre d'ack → ne pas faire croire qu'il y a un nouvel incident.
- **Couverture** : test12.

---

## 4. Heartbeat et connexion

### INV-ANDROID-300 [C] ⚠️ Heartbeat envoyé toutes les 3s au POST /api/devices/heartbeat
`AlarmPollingService.startHeartbeat()` boucle `while (isActive) { heartbeat; delay(3000) }`.
- **Pourquoi** : le watchdog backend marque offline à 60s (INV-041 backend) → 3s laisse 20 tentatives avant de basculer offline.
- **Couverture** : test05, test06 (via IdlingResource sur 2 heartbeats).

### INV-ANDROID-301 [H] ⚠️ Icône connexion : ✅ si heartbeat 2xx, ❌ sinon
Champ `connectionStatus` sur DashboardActivity affiche ✅ ou ❌ selon `AlarmPollingService.lastHeartbeatOk`.
- **Pourquoi** : feedback immédiat de l'état réseau.
- **Couverture** : test05 (OK), test06 (fail).

### INV-ANDROID-302 [C] ⚠️ Perte heartbeat > 2 min → alerte permanente + sonnerie locale
`heartbeatLossTimeoutMs = 120_000L` (configurable pour tests). Si `elapsed >= timeout` depuis `heartbeatLostSince`, `heartbeatLostAlarm = true`. La DashboardActivity déclenche alors bandeau + sonnerie via `connectionLostSoundManager`.
- **Pourquoi** : si l'appareil ne communique plus depuis 2 min, on peut rater une alarme qui s'accumule côté backend. L'utilisateur doit SAVOIR et agir.
- **Couverture** : test09 (avec timeout réduit à 3s pour la vitesse).

### INV-ANDROID-303 [H] ⚠️ Heartbeat qui revient avant timeout → reset, pas d'alerte
Un `heartbeat.isSuccessful` reset `heartbeatLostSince=0L` et `heartbeatLostAlarm=false`. Pas d'alerte si glitch réseau passager.
- **Pourquoi** : éviter les faux positifs sur passage sous tunnel / changement wifi/4G.
- **Couverture** : test10.

### INV-ANDROID-304 [M] ⚠️ Heartbeat 503 (replica) → flag needsUrlSwitch au poll suivant
Cas spécial : `response.code() == 503` signifie "backend est un replica, pas le primary". Le heartbeat set `needsUrlSwitch=true` (volatile) — le poll suivant détecte le flag et fait `switchToNextUrl()` + delay 4s pour laisser le heartbeat revalider.
- **Pourquoi** : suivre automatiquement le primary après failover Patroni côté backend. Le heartbeat ne doit PAS lui-même switcher (il suit passivement l'URL courante).
- **Manque** : test Espresso dédié au scénario 503 heartbeat (test22 couvre les 503 polling, pas heartbeat).

---

## 5. Failover backend

### INV-ANDROID-400 [C] ⚠️ 3 échecs consécutifs de polling → switchToNextUrl()
`ApiClient.consecutiveFailures >= 3` dans `onPollFailure` → `switchToNextUrl()`. Seul le polling incrémente ce compteur (pas le heartbeat).
- **Pourquoi** : rotation automatique vers le noeud sain en cas de panne primary. Pas trop tôt (1 échec = glitch réseau) ni trop tard (10 échecs = 30s d'alarmes manquées).
- **Couverture** : test22 (3 × 503 → currentUrlIndex passe de 0 à 1).

### INV-ANDROID-401 [C] ⚠️ switchToNextUrl() reset compteur + purge connexions idle
Dans `ApiClient.switchToNextUrl()` : `consecutiveFailures=0`, `connectionPool.evictAll()`.
- **Pourquoi** : éviter les `Connection reset` sur sockets obsolètes vers l'ancienne URL (caché par OkHttp pool).

### INV-ANDROID-402 [H] ⚠️ Rotation circulaire sur 3 URLs : `(currentUrlIndex + 1) % 3`
`BACKEND_URLS.size == 3` (primary + fallback1 + fallback2 configurés via BuildConfig). La rotation revient à 0 après 2.
- **Pourquoi** : cluster 3 noeuds HA. Si les 3 sont down, l'app continue à tenter à intervalle régulier.

### INV-ANDROID-403 [M] ⚠️ Succès 2xx (poll ou heartbeat) → consecutiveFailures reset à 0
Le compteur se remet à 0 dès qu'un appel réussit. Un pattern `fail fail success fail fail` ne déclenche pas switch.

### INV-ANDROID-404 [M] ❌ Reset index URL à 0 au démarrage de l'app (MainActivity.onCreate)
`ApiClient.currentUrlIndex = 0; consecutiveFailures = 0` au `onCreate` de MainActivity.
- **Pourquoi** : redémarrer "frais" à chaque lancement, même si la session précédente avait basculé.
- **Manque** : aucun test ne vérifie ce reset (test22 reset manuellement dans le @Test).

### INV-ANDROID-405 [M] ⚠️ Login tente les 3 URLs avant d'abandonner
MainActivity : si le 1er login échoue, boucle `for (i in 0 until 3) { switchToNextUrl(); login(); break if success }`.
- **Pourquoi** : cluster peut être partiellement down au moment du login. L'utilisateur ne doit pas avoir à taper manuellement sur "réessayer".

---

## 6. Auth et session

### INV-ANDROID-500 [C] ⚠️ Token stocké en SharedPreferences `alarm_prefs`
Clés : `token`, `user_name`, `user_id`, `is_oncall`, `escalation_position`, `device_token`, `started_by_fcm`.
- **Pourquoi** : persistance entre lancements app.
- **Sécurité** : SharedPreferences n'est PAS chiffré par défaut. Threat model : si le device est compromis (root), le token est accessible. Acceptable car le risque est avant tout l'alarme manquée, pas la fuite du token (qui expire et peut être révoqué côté backend).

### INV-ANDROID-501 [C] ⚠️ Login pré-rempli UNIQUEMENT en BuildConfig.DEBUG
MainActivity : `if (BuildConfig.DEBUG) { nameInput.setText("user1"); passwordInput.setText("user123") }`. En release, les champs sont vides.
- **Pourquoi** : confort dev ; interdit de committer des credentials actifs dans un APK release.

### INV-ANDROID-502 [C] ⚠️ Logout = clear prefs + stop services + retour MainActivity
`performLogout()` : (1) appel asynchrone à `deleteFcmToken`, (2) `prefs.edit().clear().apply()`, (3) `stopPollingService()`, (4) `soundManager.stopAlarmSound()` + `connectionLostSoundManager.stopAlarmSound()`, (5) `startActivity(MainActivity)`, `finish()`.
- **Pourquoi** : nettoyage complet pour éviter qu'une session zombie continue de tourner en arrière-plan.
- **Couverture** : test14 (assert token null après logout, login fonctionne ensuite).

### INV-ANDROID-503 [C] ⚠️ Logout appelle DELETE /api/devices/fcm-token avant clear
Avant le `clear()`, `ApiProvider.service.deleteFcmToken(auth, FcmTokenDeleteRequest(deviceId))` est appelé en `lifecycleScope.launch` (fire-and-forget, pas d'await).
- **Pourquoi** : retirer l'appareil du destinataire FCM côté backend → plus de push envoyé à ce device.
- **Caveat connu** : appel async → si l'app crashe ou perd le réseau avant l'envoi, le token reste enregistré côté backend. Le backend devrait tolérer ça (push envoyé à un device déconnecté = ignoré par FCM).

### INV-ANDROID-504 [H] ⚠️ Refresh token automatique toutes les 12h
`tokenRefreshIntervalMs = 12 * 60 * 60 * 1000L` (12h, hardcodé). Coroutine dédiée `startTokenRefresh()` : `while (isActive) { delay(interval); tryRefreshToken() }`.
- **Pourquoi** : garder la session valide sans forcer l'utilisateur à se relogger. 12h couvre largement une astreinte nuit.
- **Couverture** : test16 (avec override à 3s pour tester 2 cycles en 6s).

### INV-ANDROID-505 [C] ⚠️ Refresh 2xx → nouveau token en prefs + en mémoire service
Sur `refreshToken.isSuccessful`, `newToken = body.access_token` → `prefs.putString("token", newToken)` + `this.token = newToken`.
- **Pourquoi** : polling/heartbeat suivants utilisent immédiatement le nouveau token, pas de 401 intermédiaire.

### INV-ANDROID-506 [C] ⚠️ Refresh échoué → sonnerie locale + message permanent, PAS logout silencieux
`forceLogout()` NE force PAS de retour à MainActivity. Au lieu : `authErrorAlarm=true`, `authErrorMessage="Votre session a expiré et n'a pas pu être renouvelée. Veuillez vous reconnecter."`, bandeau permanent visible, sonnerie démarrée, bouton "Reconnexion" visible.
- **Pourquoi** : un logout silencieux = l'utilisateur manque des alarmes sans savoir pourquoi. Mieux vaut une alerte bruyante qui le force à agir.
- **Couverture** : test17.

### INV-ANDROID-507 [M] ⚠️ Token valide en prefs au démarrage → direct au dashboard (pas de re-login)
MainActivity : `savedToken != null → goToDashboard(savedToken)` + `return` (skip login UI).
- **Pourquoi** : UX. Relogin forcé uniquement si token absent (après logout explicite ou premier lancement).

---

## 7. FCM et push

### INV-ANDROID-600 [C] ⚠️ Push FCM avec alarm_id → démarre le foreground service en mode veille
`AlarmFirebaseService.onMessageReceived` → si `data["alarm_id"] != null`, delegue à `AlarmWakeUpHandler.onAlarmPushReceived(context, alarmId, title, severity)` → `startForegroundService(AlarmPollingService, started_by_fcm=true)` si pas déjà running.
- **Pourquoi** : les users NON-astreinte ne maintiennent pas le foreground service actif (économie batterie). FCM haute priorité les réveille quand une alarme tombe sur leur position dans la chaîne.
- **Manque** : aucun test n'injecte un push FCM et vérifie le démarrage du service. Testable via `AlarmWakeUpHandler` en isolation (pas Firebase).

### INV-ANDROID-601 [H] ⚠️ FCM type=escalation_update met à jour escalation_position + is_oncall en prefs
`if (msgType == "escalation_update")` → `prefs.putInt("escalation_position", position).putBoolean("is_oncall", isOncall)`, early return (pas de démarrage service).
- **Pourquoi** : quand un admin modifie la chaîne d'escalade, le backend envoie un push FCM à chaque user affecté pour qu'ils voient leur nouvelle position **sans relancer l'app**. Le badge se met à jour au prochain tick de `updateStatus()` (1Hz).
- **Manque** : aucun test ne simule la réception de ce push.

### INV-ANDROID-602 [H] ⚠️ FCM push reçu alors que service déjà running → ignoré
`if (!AlarmPollingService.isRunning) { start }` → sinon juste un log. Le polling en cours captera l'alarme au prochain tick (3s max).
- **Pourquoi** : éviter de redémarrer un service déjà sain. Évite aussi les race conditions (double-start).

### INV-ANDROID-603 [H] ❌ FCM onNewToken → re-registration automatique côté backend
`AlarmFirebaseService.onNewToken(newToken)` → `registerFcmToken(authToken, FcmTokenRequest(token, deviceId))` via `scope.launch`. Silencieux : pas de notification à l'utilisateur.
- **Pourquoi** : FCM peut régénérer le token sans prévenir l'app (réinstall, reset cloud messaging). Si le backend garde l'ancien, les push n'arrivent plus.
- **Couverture** : aucune (difficile à tester sans Firebase live).

### INV-ANDROID-604 [M] ❌ Service démarré par FCM + alarme résolue + non-astreinte → stopSelf
Dans la boucle polling, si `alarms.isEmpty() && startedByFcm && !isOncall` → `prefs.putBoolean("started_by_fcm", false)` + `stopSelf()`. Le service ne consomme pas de batterie en continu pour les users en mode veille.
- **Pourquoi** : équilibre batterie / résilience. L'incident est fini, le user retourne en "dormance", FCM le réveillera au prochain.
- **Manque** : aucun test Espresso (testable via `AlarmPollingService` avec mock polling).

### INV-ANDROID-605 [L] ⚠️ Token FCM enregistré au login (DashboardActivity) et re-confirmé au onCreate
Double registration : `MainActivity.login()` et `DashboardActivity.onCreate()` appellent `registerFcmToken`. Idempotent côté backend (upsert par `device_id`).
- **Pourquoi** : robustesse. Si la première registration a foiré (réseau), le dashboard réessaye.

---

## 8. Plateforme Android

### INV-ANDROID-700 [C] ⚠️ Rotation bloquée en portrait sur MainActivity et DashboardActivity
Manifest : `android:screenOrientation="portrait"` + `android:configChanges="orientation|screenSize"` → l'activité n'est ni recréée ni tournée par le système.
- **Pourquoi** : layout optimisé portrait. Landscape casse la hero card et le timeline historique.
- **Caveat** : AlarmActivity N'a PAS cette restriction (pas de `screenOrientation` dans son manifest). Comportement voulu ? Voir questions ouvertes.
- **Couverture** : test13 force rotation landscape, vérifie `requestedOrientation == SCREEN_ORIENTATION_PORTRAIT` et que l'état UI est préservé (titre d'alarme visible après tentative rotation).

### INV-ANDROID-701 [C] ⚠️ Foreground service requis pour le polling (Android 8+ compatible)
`AlarmPollingService` démarre via `startForegroundService()` + `startForeground(NOTIFICATION_ID, notification)` avec `IMPORTANCE_LOW` (notification discrète). `foregroundServiceType="specialUse"` dans manifest.
- **Pourquoi** : Android 8+ tue les background services en quelques minutes. Seul un foreground service survit en fond avec une notification persistante.

### INV-ANDROID-702 [H] ⚠️ Full-screen intent pour afficher AlarmActivity depuis le service
Notification construite avec `setFullScreenIntent(pi, true)` + `PRIORITY_MAX` + `CATEGORY_ALARM` + channel `IMPORTANCE_HIGH` + `setBypassDnd(true)` + `lockscreenVisibility=PUBLIC`.
- **Pourquoi** : mécanisme officiel Android 10+ pour forcer l'apparition d'une activité urgente par-dessus tout (y compris lockscreen). Nécessite permission `USE_FULL_SCREEN_INTENT`.

### INV-ANDROID-703 [C] ⚠️ Permission POST_NOTIFICATIONS demandée à l'utilisateur sur Android 13+
DashboardActivity `onCreate` : si `SDK >= TIRAMISU` et non grantée, `requestPermissions([POST_NOTIFICATIONS], 100)`.
- **Pourquoi** : sans cette permission, aucune notification ne sort → full-screen intent ignoré → AlarmActivity non lancée par le service → alarme manquée.
- **Limite** : si l'utilisateur refuse, l'app continue mais les push sont muets. Voir questions ouvertes.

### INV-ANDROID-704 [C] 🐛 Reprise post-boot NON implémentée
Aucun `BootReceiver` dans AndroidManifest, aucune permission `RECEIVE_BOOT_COMPLETED`. Après un reboot du device, le foreground service n'est **PAS** redémarré. L'utilisateur doit rouvrir l'app manuellement pour que le polling reprenne.
- **Pourquoi (bug)** : un user de garde qui reboot son téléphone la nuit ne reçoit plus d'alarmes via polling jusqu'à la réouverture manuelle. FCM haute priorité PEUT éveiller l'app à condition que Google Play Services soit chargé, mais c'est fragile et non garanti après un reboot à froid.
- **Fix proposé** : `BootReceiver extends BroadcastReceiver` avec action `android.intent.action.BOOT_COMPLETED`, qui démarre `AlarmPollingService` si un token est présent en prefs (et `is_oncall=true`).
- **Manque** : pas de test Espresso (nécessite simuler reboot avec `adb reboot` + attendre).

---

## 9. Tests et isolation

### INV-ANDROID-800 [C] ⚠️ Tests Espresso utilisent FakeApiService via ApiProvider.override()
`@Before` : `ApiProvider.override(FakeApiService())`. `@After` : `ApiProvider.reset()`. Aucun backend requis.
- **Pourquoi** : tests Android isolés du cluster. Pas besoin de Docker Compose. Gain énorme en vitesse et stabilité.
- **Couverture** : tous les 22 tests respectent.

### INV-ANDROID-801 [C] ⚠️ Tests préfèrent IdlingResources à Thread.sleep
`PollingIdlingResource(targetCalls=N)` notifié par `FakeApiService.pollingIdlingResource?.onApiCallComplete()` à chaque `getMyAlarms`. Espresso attend automatiquement.
- **Pourquoi** : déterministe, pas de flakiness. `Thread.sleep(5000)` peut passer ou échouer selon la charge CPU.
- **Exceptions documentées** : test10 (6s pour timing scénario), test15 (Thread.sleep entre pressHome et retour), test16 (6s pour 2 cycles refresh), test17 (10s pour détection auth failure). À minimiser.

### INV-ANDROID-802 [C] ⚠️ Pas de coordonnées (x, y) hardcodées pour les clics
Utiliser `onView(withId(R.id.xxx)).perform(click())`, jamais `uiDevice.click(x, y)`. Les rotations à `uiDevice.setOrientationLeft/Natural` sont OK (pas de coordonnées de layout).
- **Pourquoi** : résilience aux resolutions différentes (test peut tourner sur émulateurs 1080p ou 720p sans casser).

### INV-ANDROID-803 [C] ⚠️ Pas de MockK — uniquement FakeApiService écrit à la main
Problème connu : MockK casse sur Dalvik/ART dans ce projet (pas investigué en profondeur, décision CLAUDE.md). Utiliser un fake implémenté à la main, plus lisible et compatible.
- **Pourquoi** : fiabilité de la suite. Un mock qui crash en Dalvik = toute la suite rouge pour une mauvaise raison.

---

## Comment ajouter un invariant

1. Identifier la **règle business** (pas l'implémentation).
2. Numéroter avec le prochain ID libre dans la section.
3. Préciser :
    - Description (1 phrase claire)
    - **Pourquoi** (motivation business, pas technique)
    - **Couverture** : tests Espresso qui le couvrent (avec statut ⚠️ si partiel)
    - **Manque** : ce qui reste à tester pour passer ⚠️ → ✅
    - Criticité + statut.
4. Si ambigu, créer une entrée dans **❓ Questions restantes** et demander au propriétaire AVANT d'écrire des tests.
5. Commit : le catalogue fait partie de l'histoire du produit, pas un doc jetable.

## Comment supprimer un invariant

Si le business change (ex: on autorise le landscape), NE PAS supprimer l'invariant silencieusement :
1. PR qui modifie l'invariant avec justification commit.
2. Les tests associés doivent être supprimés DANS LE MÊME PR.
3. Le code doit être modifié DANS LE MÊME PR.

Sinon → tests orphelins qui verrouillent un comportement mort, ou invariants obsolètes qui désinforment la prochaine session IA.

---

## ❓ Questions restantes au propriétaire

Les questions ci-dessous sont des ambiguïtés rencontrées pendant la rédaction. À
trancher avant d'écrire des tests sur ces aires, sous peine de figer un
comportement non voulu (cf P1 de `docs/AI_STRATEGY.md`).

1. **POST_NOTIFICATIONS refusé par l'utilisateur (Android 13+)** : l'app continue à tourner mais aucune notification ne sort → full-screen intent ignoré → **alarme totalement muette**. Que doit-on faire ?
   - Option A : bandeau permanent "Notifications requises — l'app ne peut pas vous alerter" avec CTA vers les paramètres.
   - Option B : retenter la demande de permission à chaque ouverture de DashboardActivity.
   - Option C : fallback sur vibration continue + chip écran "ALARME en cours" au cas où l'utilisateur regarde quand même l'app.
   - Non clarifié aujourd'hui → **aucun invariant écrit sur ce sujet**.

2. **BootReceiver (INV-ANDROID-704 🐛)** : faut-il vraiment redémarrer le foreground service après reboot, ou le push FCM high-priority est-il considéré suffisant ? La doc Android dit "FCM peut contourner Doze après reboot à froid" mais c'est fragile (Google Play Services doit être ready). Décision = implémenter un BootReceiver ?

3. **Doze mode / App Standby** : pour les users en astreinte (foreground service actif), Doze ne devrait pas s'appliquer. Mais Android peut mettre l'app en "App Standby Bucket" si l'utilisateur ne l'ouvre pas pendant X jours. Le foreground service continue-t-il ? Y a-t-il un hardening à prévoir (requête de dispense de restriction batterie au user) ?

4. **`tokenRefreshIntervalMs = 12h` hardcodé** : cohérent avec INV-084 backend qui demande que tous les délais soient paramétrables via SystemConfig. Migrer vers une clé `SystemConfig.android_token_refresh_hours` ? Ou laisser côté client pour réduire le couplage ?

5. **AlarmActivity sans restriction d'orientation** : manifest AlarmActivity n'a pas `screenOrientation="portrait"`. Est-ce intentionnel (lock screen peut être en landscape sur certains devices) ou un oubli ?

6. **AlarmActivity re-launch pendant qu'une autre est déjà visible** : code actuel `alarm.id != alarmActivityLaunchedForId && !AlarmActivity.isVisible`. Si une 2e alarme arrive (ID différent) pendant qu'AlarmActivity affiche la 1ère → la 2e N'EST PAS affichée plein écran. L'utilisateur doit acquitter la 1ère pour voir la 2e. Est-ce voulu ? Ou devrait-on empiler / mettre à jour l'activité avec la dernière alarme ?

7. **Google Play Services absent** (devices sans Google ou firewall corporate) : FCM ne fonctionne pas → les users en mode veille ne reçoivent aucune alarme jusqu'à réouverture manuelle. Fallback ? Polling permanent pour les users non-astreinte aussi ? SMS côté backend ?

8. **Test `test15_alarmReceivedWhileInBackground`** : le `UiSelector().descriptionContains("Alarme Critique")` utilise l'ancien nom "Alarme Critique", alors que l'app s'appelle "Alarme Murgat" depuis le rebrand (cf CLAUDE.md). Le test passe quand même car il a un fallback via intent. À corriger pour éviter de figer l'ancien nom.

9. **Notification channel ID `alarm_critical_channel`** (code) vs nom d'app "Alarme Murgat" : l'ID technique reste `critical` — OK si on le documente. Mais faut-il migrer à `alarm_murgat_channel` pour la cohérence ? Attention à la migration (l'ancien channel peut rester en prefs utilisateur).

---

## 📊 Backlog suggéré de fix et tests Android

Ordre recommandé pour les prochaines itérations (à arbitrer) :

| INV | Criticité | Complexité | Note |
|---|---|---|---|
| INV-ANDROID-704 | C | ★★ | Implémenter `BootReceiver` + permission `RECEIVE_BOOT_COMPLETED` + test avec simulation de boot. Résout un bug d'alarme manquée après reboot. |
| INV-ANDROID-003 | C | ★ | Ajouter test qui vérifie `audioManager.getStreamVolume(STREAM_ALARM)` après déclenchement. Trivial, renforcerait la suite. |
| INV-ANDROID-007 | C | ★ | Ajouter test qui vérifie `connectionLostSoundManager.isPlaying()` après `heartbeatLostAlarm=true`. |
| INV-ANDROID-600, 601 | C/H | ★★ | Tests unitaires sur `AlarmWakeUpHandler` (pas besoin de Firebase) + mock context. Cover FCM reception. |
| INV-ANDROID-107 | M | ★★ | Test qui vérifie bouton helpFab + intercepte l'Intent.ACTION_SEND (Espresso-Intents). |
| INV-ANDROID-404 | M | ★ | Test qui simule switch URL en session puis relance l'app → vérifier que index revient à 0. |
| (questions 1-9) | — | — | Trancher avec le propriétaire avant d'écrire des tests sur ces sujets. |

---

## Dernière mise à jour

- **2026-04-21** : création du document. Synthèse de la lecture du code `android/app/src/main/**` + des 22 tests Espresso `AlarmE2ETest.kt`. 58 invariants rédigés, 9 questions ouvertes. Inspiré du format `tests/INVARIANTS.md` (backend) pour homogénéité.
