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

### INV-ANDROID-003 [C] ⚠️ Volume forcé à 50% du maximum sur le stream alarme (révisé 2026-06-04)
À chaque `startAlarmSound()`, `audioManager.setStreamVolume(STREAM_ALARM, (maxVol * 0.5f).toInt(), 0)` est appelé. Le même code sert pour la sonnerie alarme métier et pour la sonnerie « hors connexion » (cf INV-ANDROID-007).
- **Pourquoi** : si l'utilisateur a baissé son volume la veille, la sonnerie doit quand même être audible. Le stream ALARM est par design non affecté par le mode silencieux. La cible 50% est un compromis entre audibilité (toujours réveille) et agressivité ergonomique (le 100% saturait à l'oreille sur les appareils d'astreinte testés Crosscall où max=15).
- **Changement 2026-06-04** : valeur passée de `maxVol` à `(maxVol * 0.5f).toInt()`. Décision business propriétaire après retour terrain « trop fort, agressif au réveil ». 50% reste loin au-dessus du seuil de réveil.
- **Effet de bord persistant** : `setStreamVolume` écrit la valeur dans le réglage système (pas juste le lecteur). Si l'utilisateur ajuste manuellement, la prochaine sonnerie rebascule à 50%. `stopAlarmSound()` ne restaure pas l'ancienne valeur (inchangé vs version max).
- **Manque** : aucun test ne vérifie le volume du stream après déclenchement (manque préexistant, indépendant du changement de cible).

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
- **Note 2026-06-03** (révision) : la gâchette de `heartbeatLostAlarm` est définie par INV-ANDROID-308 (absence de SMS `[ALARME-MURGAT-PING]` pendant 5 min après perte heartbeat HTTP). Le **canal sonore et son comportement sont inchangés** — seule la condition d'armement évolue (l'ancienne formulation 2026-05-26 « heartbeat + noNetwork au sens INV-305 » est obsolète depuis le pivot INV-308). `authErrorAlarm` reste indépendant : la sonnerie sur erreur d'auth ne dépend PAS de l'état réseau (token expiré = action manuelle requise quelle que soit la connectivité).
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

### INV-ANDROID-104 [H] ⚠️ Alerte "Connexion perdue" : bandeau permanent, pas toast — découplé de la sonnerie (révisé 2026-06-03 sur INV-308)
Le bandeau "Connexion perdue avec le serveur" doit apparaître **dès que le heartbeat est perdu** (sans attendre la condition de sonnerie d'INV-ANDROID-308). La sonnerie locale, elle, n'est armée que si **aucun SMS `[ALARME-MURGAT-PING]` du backend** n'est arrivé dans les 5 min suivant la perte de heartbeat.

2 drapeaux distincts dans `AlarmPollingService` :
- `heartbeatLostVisual` (info) → `elapsed >= 120 000ms` depuis dernier heartbeat 2xx → bandeau VISIBLE, **pas de son**.
- `heartbeatLostAlarm` (action) → `heartbeatLostVisual && elapsed >= 5*60_000ms && lastPingReceivedElapsedMs < heartbeatLostSince` (cf INV-308) → bandeau VISIBLE + sonnerie locale.
- `authErrorAlarm` (inchangé) → message personnalisé, bouton "Reconnexion" VISIBLE, sonnerie déclenchée (indépendant de l'état réseau, cf INV-007).

Textes du bandeau :
- `heartbeatLostVisual` sans condition de sonnerie atteinte → "Serveur injoignable — vous recevrez les alarmes par SMS si nécessaire" (texte rassurant, pas de panique). C'est aussi le texte pendant la fenêtre 5 min si au moins un SMS PING est arrivé après `heartbeatLostSince`.
- `heartbeatLostAlarm` (5 min écoulées sans aucun SMS PING) → "Connexion perdue — déplacez-vous pour retrouver du réseau" (texte d'action, accompagné de la sonnerie).
- `authErrorAlarm` → message personnalisé (ex: "Votre session a expiré..."), bouton "Reconnexion" VISIBLE.

- **Pourquoi** : un toast qui disparaît en 3s = l'utilisateur ne le voit pas s'il n'a pas le téléphone en main. Un bandeau permanent force la lecture. Distinguer info-only vs action-required évite le cri-loup.
- **Diagnostic cause d'échec (2026-06-15)** : `onHeartbeatFail(reason)` reçoit désormais la cause de l'échec (`"HTTP 401"`, `"HTTP 500"`, `"reseau KO (<msg>)"`) depuis l'appelant (`startHeartbeat`), qui l'extrait de `response.code()` ou `e.message`. Avant, `onHeartbeatFail()` était appelée sans argument → l'info était jetée, impossible de savoir pourquoi le heartbeat tombait (ex: heartbeat KO juste après un login réussi sur le même backend). La cause est loguée dans `AppLogger` sur **transition seulement** (1er échec, passage des 120s) **+ changement de cause** (ex: `HTTP 401 → reseau KO`), jamais à chaque tick — le heartbeat tape à 3s, logger chaque échec saturerait le buffer 500 entrées en ~25 min. La variable companion `lastHeartbeatFailReason` mémorise la dernière cause pour détecter les changements. Le 503 (replica) garde son comportement (`needsUrlSwitch`, pas d'armement de perte) mais est tracé une fois par transition.
- **Statut** : architecture INV-308 (absence SMS) à implémenter — le code 2026-05-27 (drapeaux companion dans `AlarmPollingService.kt`) reste structurellement valable, seule la condition d'armement de `heartbeatLostAlarm` change (consulte `lastPingReceivedElapsedMs` au lieu de `NetworkAvailabilityMonitor.isNoNetwork`). Tracé cause d'échec ajouté 2026-06-15.
- **Couverture** : ⚠️ tests Espresso à étoffer (test09 actuel ne distingue pas encore les 2 textes, ni le tracé de cause). À ajouter dans la PR d'implémentation INV-308.

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

### INV-ANDROID-108 [M] ❌ Bouton "Envoyer les logs" sur la page de login (pré-auth) + diagnostic login tracé — révisé 2026-06-15
`shareLogsButton.setOnClickListener { shareLogs() }` sur `MainActivity` → mêmes `AppLogger.exportLogs(...)` + `Intent.ACTION_SEND` que INV-ANDROID-107. `user_name` = `"(non connecté)"` dans l'en-tête du export puisque la session n'a pas encore d'identité confirmée.
- **Pourquoi** : INV-ANDROID-107 n'est accessible qu'une fois loggé. Si le login échoue (URL backend inaccessible, timeout réseau, mauvais credentials), l'opérateur ne pouvait pas exporter les logs depuis l'app — il fallait passer par ADB ou re-essayer indéfiniment. Le bouton login est explicitement conçu pour les diagnostics réseau au démarrage (rotation des URLs cluster, certificats, DNS).
- **Tracé du flow login (2026-06-15)** : `MainActivity.login()` émet des events `AppLogger.log("Login", ...)` à chaque étape — tentative (user + URL courante), résultat de chaque essai (`HTTP <code>` ou `reseau KO (<msg>)`) y compris pendant la rotation des 3 URLs, succès (user/oncall/pos), ou échec définitif avec le code et le message affiché. Avant ce changement, aucune trace côté `AppLogger` : un login en échec (401, timeout, serveur down) n'apparaissait PAS dans l'export, rendant le bouton inutile pour diagnostiquer justement le cas qu'il vise.
- **Message clair (2026-06-15)** : `loginErrorMessage(code)` traduit la cause en message lisible (`401` → "Identifiants incorrects (nom ou mot de passe)", `429` → "Trop de tentatives — réessayez dans 1 minute", `5xx` → "Serveur en erreur", `null`/réseau → "Serveur injoignable — vérifiez votre connexion réseau"). Remplace l'ancien `"Échec de connexion : 401"` cryptique. `ApiClient.currentBaseUrl()` ajouté pour exposer l'URL backend courante aux logs.
- **Privacy (anti-fuite cross-user)** : `AppLogger.clear()` est appelé dans la procédure logout de `DashboardActivity` (cf code) **avant** de revenir à `MainActivity`. Garantit que le contenu de `AppLogger` au moment du clic « Envoyer les logs » sur l'écran de login ne contient QUE les events de la session courante (pré-login) ou ceux du démarrage app si pas de session précédente — jamais ceux d'un user A précédemment déconnecté.
- **Statut** : code implémenté 2026-06-04, tracé login + messages clairs 2026-06-15 (cf `MainActivity.login()` / `loginErrorMessage()` / `shareLogs()` + `ApiClient.currentBaseUrl()` + `AppLogger.clear()` dans `DashboardActivity.logout()`).
- **Manque** : aucun test ne vérifie la présence du bouton, l'envoi du share intent, le clear au logout, ni les events de login tracés. Tests Espresso à ajouter dans une PR follow-up.

### INV-ANDROID-109 [H] ❌ Version de build dérivée automatiquement de git (anti-bump-manquant) — 2026-06-12
`versionCode` et `versionName` du `defaultConfig` Gradle sont calculés au build time depuis git, pas hardcodés :
- `versionCode = git rev-list --count HEAD` (nombre total de commits, monotone)
- `versionName = "${baseVersion}.${gitCommitCount}-${gitShortSha}[-dirty]"` (ex: `1.0.188-b95d44b` ou `1.0.188-b95d44b-dirty` si l'arbre n'est pas propre)
- Si git inaccessible (rare, build hors repo) : fallback `versionCode=1`, `versionName="1.0-unknown"`

La version apparaît dans **toutes les exports `AppLogger.exportLogs()`** via l'en-tête (cf INV-ANDROID-107/108). Permet de tracer une session loguée à un commit exact.
- **Pourquoi** : élimine la classe entière de bugs « j'ai oublié de bumper la version dans build.gradle » — chaque commit produit automatiquement une version distincte et traçable. Avant, `versionCode=1` et `versionName="1.0"` étaient hardcodés : deux APK construits à des moments différents avaient la même version, impossible de savoir lequel produisait les logs ou les remontées Crashlytics.
- **Trade-off `-dirty`** : un build avec working tree non-commité hérite du sha du dernier commit + suffixe `-dirty`. Volontaire : on voit immédiatement qu'un APK ne correspond à aucun commit pushé (utile en debug local, à éviter en distribution).
- **Pas d'effet sur la signature** : la build n'utilise toujours pas de signing config explicite ; les release-builds sont signés en post avec la debug-keystore (cf scripts/build_apk_release.sh à venir).
- **Statut** : implémenté 2026-06-12 dans `android/app/build.gradle.kts` (`gitOutput()` helper + 3 `val`s en tête de fichier). Build local vérifié : versionCode=188, versionName="1.0.188-b95d44b-dirty".
- **Manque** : pas de test (la valeur dépend de l'environnement git, difficile à fixer dans un test unitaire). Acceptable car la logique est triviale et le smoke-test « build l'APK et lis le manifest » suffit.

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

### INV-ANDROID-302 [C] ❌ SUPERSEDED (2026-06-03) — remplacé par INV-ANDROID-308 (absence de SMS ping)

> **Pourquoi déprécié** : la détection "no network at all" via `NetworkAvailabilityMonitor` (INV-305) s'est avérée impossible à implémenter de façon fiable sur Android moderne (Crosscall Core-M6 / Android 15) — les APIs telephony.* sont redactées ou les callbacks ne tirent pas en mode avion. Le déclencheur business "l'opérateur est hors d'atteinte de tous les canaux" est désormais réalisé côté tél par l'absence de SMS `[ALARME-MURGAT-PING]` du backend dans les 5 min suivant la perte de heartbeat (cf INV-ANDROID-308 nouvelle version 2026-06-03). Le code Kotlin actuel reste en place comme fallback transitoire mais sera supprimé dans la même PR qui implémente INV-308 nouveau.

### INV-ANDROID-302-LEGACY [C] ⚠️ (legacy, à supprimer) Sonnerie locale armée uniquement si perte heartbeat > 2 min ET réseau totalement perdu
`heartbeatLossTimeoutMs = 120_000L` (configurable pour tests). Au-delà de ce délai depuis `heartbeatLostSince`, on évalue la connectivité réseau du téléphone (cf INV-ANDROID-305) :
- **réseau présent** (data 4G/Wi-Fi disponible **OU** signal cellulaire voix/SMS disponible) → `heartbeatLostAlarm` reste `false`. Le bandeau visuel "Serveur injoignable" reste affiché via `heartbeatLostSince` (cf INV-ANDROID-104), mais **aucune sonnerie locale** n'est démarrée.
- **réseau totalement perdu** (ni data **ni** signal cellulaire) → `heartbeatLostAlarm = true`, `connectionLostSoundManager.startAlarmSound()` démarre.
Le drapeau retombe à `false` dès que (a) un heartbeat repasse 2xx (cf INV-ANDROID-303), **OU** (b) l'un des deux canaux réseau redevient disponible — auquel cas le téléphone est à nouveau joignable par le système d'astreinte (SMS via gateway ou push différé) et la sonnerie n'a plus de raison d'être.
- **Pourquoi** : la sonnerie locale a pour seul but de signaler à l'opérateur **qu'il est lui-même hors de portée de tous les canaux** et doit donc bouger pour retrouver du réseau. Si le téléphone a du SMS ou de la data, le système peut le joindre d'une manière ou d'une autre (SMS gateway, push différé quand connexion revient) — la sonnerie est anxiogène et inutile. Cas typiques de faux positifs aujourd'hui : backend en panne courte, maintenance planifiée, tunnel/changement Wi-Fi avec rétablissement < 10 min. Ces cas restent visibles par le bandeau, sans réveiller l'opérateur.
- **Statut** : code implémenté 2026-05-27 — `AlarmPollingService.onHeartbeatFail()` consulte `NetworkAvailabilityMonitor.isNoNetwork` (cf INV-305) avant d'armer `heartbeatLostAlarm`. Si le réseau revient (data OU cellular) alors que heartbeat reste perdu, le drapeau retombe au tick suivant — la sonnerie s'arrête sans attendre un heartbeat 2xx.
- **Couverture à compléter** : test09 (aujourd'hui : heartbeat lost ⇒ bandeau, ne vérifie pas la sonnerie) doit devenir 3 tests Espresso (à ajouter dans une PR follow-up — nécessite injection d'un `FakeNetworkMonitor` ou utilisation de `NetworkAvailabilityMonitor._setStatesForTest`) :
  - `test_heartbeat_lost_with_network_no_local_alarm` : heartbeat down + data OU cellular up → bandeau VISIBLE mais `connectionLostSoundManager.isPlaying == false`.
  - `test_heartbeat_lost_and_no_network_triggers_local_alarm` : heartbeat down + data DOWN + cellular DOWN → sonnerie démarre.
  - `test_local_alarm_stops_when_network_returns` : à partir du cas précédent, restaurer data OU cellular → sonnerie s'arrête immédiatement, sans attendre un heartbeat 2xx.
- **Dépendance** : INV-ANDROID-305 (définition formelle de "no network at all") et INV-ANDROID-007 (canal sonore réutilisé tel quel — seule la condition d'armement change).

### INV-ANDROID-303 [H] ⚠️ Heartbeat qui revient avant timeout → reset complet, pas d'alerte
Un `heartbeat.isSuccessful` reset l'intégralité de l'état "épisode hors connexion" :
- `heartbeatLostSince = 0L`
- `heartbeatLostAlarm = false`
- `heartbeatLostVisual = false`
- `lastPingReceivedElapsedMs = 0L` (cf INV-ANDROID-308 nouvelle version 2026-06-03)
- `snoozeCount = 0`, `snoozeUntilElapsedMs = 0L` (cf INV-ANDROID-307)

Pas d'alerte si glitch réseau passager.
- **Pourquoi** : éviter les faux positifs sur passage sous tunnel / changement wifi/4G.
- **Couverture** : test10.

### INV-ANDROID-304 [M] ⚠️ Heartbeat 503 (replica) → flag needsUrlSwitch au poll suivant
Cas spécial : `response.code() == 503` signifie "backend est un replica, pas le primary". Le heartbeat set `needsUrlSwitch=true` (volatile) — le poll suivant détecte le flag et fait `switchToNextUrl()` + delay 4s pour laisser le heartbeat revalider.
- **Pourquoi** : suivre automatiquement le primary après failover Patroni côté backend. Le heartbeat ne doit PAS lui-même switcher (il suit passivement l'URL courante).
- **Complément backend 2026-06-17 (cf INV-043 révisé)** : ce mécanisme de rotation est désormais surtout un filet de secours. Côté backend, **tout** nœud replica forwarde le heartbeat au leader (via WG) et renvoie 200 — sur tous les nœuds, pas seulement le cloud (résilience : si le cloud tombe et qu'un onsite devient le point d'entrée externe, il relaie aussi). Donc en pratique l'app ne voit quasiment plus de 503 sur le heartbeat : elle reçoit un 200 (direct si elle a tapé le leader, proxifié sinon). Le `needsUrlSwitch`/rotation ne se déclenche plus que dans le cas résiduel « le nœud tapé est carrément down (connexion refusée) » ou « aucun leader joignable → 503 » (panne cluster réelle).
- **Manque** : test Espresso dédié au scénario 503 heartbeat (test22 couvre les 503 polling, pas heartbeat).

### INV-ANDROID-305 [C] ❌ SUPERSEDED (2026-06-03) — détection "no network at all" abandonnée

> **Pourquoi déprécié** : APIs Android telephony.* (ServiceState, callback ServiceStateListener, polling serviceState) inutilisables sur ROMs modernes (Crosscall Android 15 : callback jamais tiré, état figé même en mode avion). Le test concret de joignabilité opérationnelle se fait désormais via la réception périodique de SMS `[ALARME-MURGAT-PING]` du backend (cf INV-ANDROID-308 nouvelle version 2026-06-03). Le code `NetworkAvailabilityMonitor.kt` reste en place comme fallback transitoire (utilisé par INV-302-LEGACY) mais sera supprimé dans la PR INV-308.

### INV-ANDROID-305-LEGACY [C] ⚠️ (legacy, à supprimer) Détection "réseau totalement perdu" = ni data ni signal cellulaire
Le téléphone est considéré "sans aucun réseau" si **les deux** conditions sont vraies simultanément :
1. **Pas de data utilisable** : `ConnectivityManager.getActiveNetwork()` retourne `null`, **ou** le `NetworkCapabilities` du réseau actif ne contient pas `NET_CAPABILITY_INTERNET` + `NET_CAPABILITY_VALIDATED`. Couvre Wi-Fi et mobile data.
2. **Pas de signal cellulaire voix/SMS** : `TelephonyManager.getServiceState().getState() != STATE_IN_SERVICE` (ou équivalent multi-SIM via `getServiceStateForSubscriber` ≥ API 30). Couvre la capacité du téléphone à envoyer/recevoir un SMS et à passer/recevoir un appel.

État évalué de façon réactive via :
- `ConnectivityManager.registerDefaultNetworkCallback()` pour data (events `onAvailable` / `onLost`).
- `TelephonyManager.registerTelephonyCallback(ServiceStateListener)` (API 31+) ou `listen(PhoneStateListener.LISTEN_SERVICE_STATE)` (legacy) pour cellulaire.
- Pas de polling périodique : on s'abonne aux callbacks au démarrage de `AlarmPollingService` et on les libère au stop.

Permissions nécessaires (déjà au manifest pour la plupart) : `ACCESS_NETWORK_STATE` (data), `READE_PHONE_STATE` (cellular service state — à ajouter si absent, runtime perm sur Android 6+).

- **Pourquoi** : c'est l'unique signal exploitable pour distinguer "le backend est inatteignable mais je peux être joint autrement" de "je suis dans un trou réseau total". Cf INV-ANDROID-302 pour l'usage.
- **Edge cases attendus** :
  - Mode avion → les 2 conditions deviennent vraies en quelques secondes → no-network = true.
  - Wi-Fi connecté sans Internet (captive portal non validé) → `NET_CAPABILITY_VALIDATED` absent → data considérée indisponible. Si cellular aussi down → no-network = true. Correct : le téléphone n'est effectivement joignable par rien.
  - Tunnel court (< 10 s) → callbacks bouncent ; tant qu'on n'arme pas la sonnerie avant 2 min de heartbeat lost (INV-302), pas de faux positif.
- **Statut** : code implémenté 2026-05-27 — singleton `util/NetworkAvailabilityMonitor.kt` initialisé par `AlarmPollingService.onCreate()` et libéré dans `onDestroy()`. `READ_PHONE_STATE` demandée au runtime dans `MainActivity.onCreate` ; refus → fail open conservateur (`cellularInService = true`, donc INV-302 ne s'armera jamais sans cette perm).
- **Couverture à créer** (PR follow-up) :
  - `test_no_network_when_airplane_mode` : activer mode avion via `UiAutomation.executeShellCommand("cmd connectivity airplane-mode enable")` → state lu doit être `noNetwork = true` sous 5 s.
  - `test_data_only_loss_is_not_no_network` : couper data, garder cellular → `noNetwork = false`.
  - `test_cellular_only_loss_is_not_no_network` : couper cellular (retirer SIM côté émulateur ou forcer via `cmd phone`), garder Wi-Fi → `noNetwork = false`.

### INV-ANDROID-306 [H] ❌ SUPERSEDED (2026-06-03) — countdown pré-sonnerie INV-302 sans objet

> **Pourquoi déprécié** : la sonnerie INV-302 elle-même est déprécié (cf ci-dessus), donc le countdown pré-sonnerie n'a plus de cible. Avec la nouvelle INV-ANDROID-308 (absence SMS pendant 5 min), un countdown équivalent serait techniquement possible (compteur de la fenêtre 5 min après `heartbeatLostSince`) mais n'est PAS spécifié dans cette V1 — l'UX préfère un déclenchement direct sans préavis (l'opérateur de garde est censé être vigilant). À reconsidérer si retour terrain l'indique.

### INV-ANDROID-306-LEGACY [H] ⚠️ (legacy, à supprimer) Countdown graphique pré-sonnerie (fenêtre 2 min vers INV-302)
Quand `heartbeatLostSince > 0L` ET `NetworkAvailabilityMonitor.isNoNetwork == true` ET `heartbeatLostAlarm == false` (= heartbeat tombé, réseau totalement perdu, mais on est encore dans la fenêtre 2 min avant que la sonnerie INV-302 ne tape), `DashboardActivity` affiche dans le bandeau "Connexion perdue" :
- un `ArcTimerView` (composant déjà utilisé pour le countdown ack, cf INV-ANDROID-203) initialisé avec `setTime(120, remainingSec)` — l'arc se vide en sens horaire.
- un label texte "Aucun réseau — la sonnerie va se déclencher dans : Xm Ys", remis à jour à 1 Hz par la boucle `statusUpdateJob`.
- aucune sonnerie locale tant que le seuil 2 min n'est pas atteint.

Dès que le réseau revient (data OU cellular) ou que le heartbeat repasse 2xx, le countdown disparaît instantanément (le bandeau bascule sur l'état INV-ANDROID-104 "Serveur injoignable" sans son, ou se masque complètement).

- **Pourquoi** : prévenir l'opérateur quelques dizaines de secondes avant que sa sonnerie hors-connexion ne le réveille, pour lui laisser le temps de retrouver une zone couverte ou d'anticiper l'inconvénient. Évite l'effet "alarme brutale sans préavis" quand on entre dans un trou réseau.
- **Statut** : code implémenté 2026-05-29 — `DashboardActivity` branche le countdown dans la même boucle qui pilote les drapeaux INV-302/104, layout `activity_dashboard.xml` ajoute `connectionLostArcTimer` + `connectionLostCountdownLabel` dans le bandeau existant.
- **Couverture à créer** (PR follow-up) :
  - `test_arc_visible_when_heartbeat_lost_and_no_network` : forcer `heartbeatLostSince = elapsedRealtime - 30s` + `NetworkAvailabilityMonitor._setStatesForTest(false, false)` → ArcTimerView VISIBLE, label "1m 30s" (avec tolérance).
  - `test_arc_disappears_when_network_returns` : à partir du cas précédent, `_setStatesForTest(true, true)` → ArcTimerView GONE au prochain tick.

### INV-ANDROID-307 [H] ⚠️ Snooze 5 min de la sonnerie locale (max 3 par épisode)
Quand la sonnerie INV-ANDROID-308 est active (`heartbeatLostAlarm == true`), un bouton "Faire taire 5 min (N restants)" est visible **uniquement si `snoozeCount < LOCAL_ALARM_SNOOZE_MAX_COUNT` (= 3)**. Au clic :
- `AlarmPollingService.snoozeLocalAlarm()` est appelée. Elle incrémente `snoozeCount`, arme `snoozeUntilElapsedMs = now + 5 min`, et set `heartbeatLostAlarm = false`.
- La sonnerie s'arrête immédiatement (`connectionLostSoundManager?.stopAlarmSound()`).
- Le bandeau reste visible avec un countdown 5 min (composant `ArcTimerView` partagé avec INV-203) et le texte "Sonnerie en sourdine — reprise prévue dans : Xm Ys".

À l'expiration du snooze, le prochain tick de `onHeartbeatFail` ré-arme `heartbeatLostAlarm` (= sonnerie redémarre) **si les conditions INV-308 sont encore vraies** (heartbeat HTTP toujours KO ET aucun SMS PING reçu après `heartbeatLostSince`). Si un heartbeat 2xx ou un SMS PING est arrivé pendant le snooze, l'épisode est désamorcé et la sonnerie ne repart pas.

Le compteur `snoozeCount` se reset à 0 **uniquement** quand un heartbeat revient 2xx (= fin d'épisode). Tant que l'épisode dure, l'opérateur peut snoozer 3 fois maximum (3 × 5 min = 15 min de répit total), après quoi le bouton disparaît et la sonnerie reste active jusqu'à action externe (retour réseau ou logout).

- **Pourquoi** : laisser à l'opérateur d'astreinte un répit court pour gérer la situation (déplacement, recherche de réseau) sans pour autant lui permettre de mettre indéfiniment son téléphone en silencieux — l'astreinte critique exige qu'il reste joignable au-delà de 15 min cumulées de snooze.
- **Statut** : code implémenté 2026-05-29 — vars `snoozeUntilElapsedMs` / `snoozeCount` dans le companion de `AlarmPollingService`, méthode `snoozeLocalAlarm()` qui renforce le quota, `onHeartbeatFail` consulte `isLocalAlarmSnoozed()` avant d'armer la sonnerie, reset dans la branche heartbeat 2xx.
- **Couverture à créer** (PR follow-up) :
  - `test_snooze_silences_alarm_5min` : armer sonnerie, cliquer snooze → `MediaPlayer.isPlaying() == false`, label "Sonnerie en sourdine".
  - `test_snooze_quota_3_times_then_button_gone` : 3 snoozes consécutifs → 4e affichage du bandeau : bouton GONE.
  - `test_snooze_resets_on_heartbeat_2xx` : armer sonnerie, snoozer, heartbeat OK → `snoozeCount == 0`, `snoozeUntilElapsedMs == 0`. Un futur épisode rouvre 3 snoozes.
- **Dépendance** : INV-ANDROID-308 (la sonnerie qu'on snooze, nouvelle version absence-de-SMS depuis 2026-06-03). Anciennement dépendait d'INV-302 et INV-306 (legacy).

### INV-ANDROID-308 [C] 🐛 Sonnerie locale "hors connexion" : **absence** de SMS du backend pendant que le heartbeat HTTP est KO (canon, 2026-06-03)

**Refonte 2026-06-03** : la version précédente (SMS WAKE déclenché par réception, mergée par #153) était logiquement morte-née (cf "Pourquoi le pivot" ci-dessous). Le déclencheur est **inversé** : la sonnerie part quand l'app **ne reçoit PAS** un SMS du backend dans une fenêtre de 5 min suivant la perte de heartbeat. Cette spec annule et remplace INV-308 #153.

**Machine d'état métier** :

| État | Côté tél | Action |
|---|---|---|
| Heartbeat HTTP 2xx récent (< 3 s, cf INV-ANDROID-300) | rien à surveiller | silence |
| Heartbeat HTTP perdu (≥ 1 échec depuis dernier 2xx) **+** SMS `[ALARME-MURGAT-PING]` reçu **après** `heartbeatLostSince` dans la fenêtre 5 min | preuve que le canal SMS marche → l'opérateur est joignable autrement, le système peut le contacter | bandeau info uniquement (cf INV-ANDROID-104 réutilisé), pas de son |
| Heartbeat HTTP perdu **+** aucun SMS `[ALARME-MURGAT-PING]` reçu dans les 5 min suivant `heartbeatLostSince` | les deux canaux sont morts (Internet ET SMS) → vraie isolation | **sonnerie locale armée**, bouton snooze visible (cf INV-ANDROID-307) |
| Heartbeat HTTP repasse 2xx | fin d'épisode | reset complet — `heartbeatLostSince=0`, `lastPingReceivedElapsedMs=0`, sonnerie stop, `snoozeCount=0` |

**Mécanisme détaillé** :
1. Le tél maintient une variable `lastPingReceivedElapsedMs: Long` (timestamp `SystemClock.elapsedRealtime()` du dernier SMS ping reçu, `0L` à l'init).
2. Un `BroadcastReceiver` static (manifest, perm `RECEIVE_SMS`) écoute `SMS_RECEIVED`. Quand un SMS matche le préfixe exact `[ALARME-MURGAT-PING]`, set `lastPingReceivedElapsedMs = elapsedRealtime()`. Pas de son déclenché ici.
3. Dans `AlarmPollingService.onHeartbeatFail` (déjà existant), au moment où `heartbeatLostSince > 0L` et `elapsed >= 5*60_000ms`, on évalue : `lastPingReceivedElapsedMs >= heartbeatLostSince` ?
   - Si **oui** (au moins un ping ≥ depuis la perte) → `heartbeatLostVisual = true` (bandeau info, cf INV-ANDROID-104), pas de sonnerie.
   - Si **non** (aucun ping depuis la perte de heartbeat) → `heartbeatLostAlarm = true` (sonnerie armée).
4. Snooze : inchangé (INV-ANDROID-307, 5 min × 3 max).
5. Reset au heartbeat 2xx : `heartbeatLostSince=0`, `lastPingReceivedElapsedMs=0`, `heartbeatLostAlarm=false`, `heartbeatLostVisual=false`, `snoozeCount=0`.

**Pourquoi le pivot (vs INV-308 #153 sens inverse)** :

La version précédente (sonner quand on **reçoit** le SMS WAKE) était logiquement circulaire : si le tél peut recevoir un SMS, c'est qu'il est joignable par SMS — donc la sonnerie d'avertissement préventive est inutile (le canal SMS d'alarmes métier INV-060 marche aussi). Si le tél ne peut pas recevoir, le SMS WAKE n'arrive pas non plus → pas de sonnerie. Aucun cas business ne justifiait cette logique.

Le pivot 2026-06-03 inverse le déclencheur : la sonnerie part sur **absence** de SMS, ce qui correspond à la situation business réelle « j'ai perdu mon heartbeat HTTP **et** je ne reçois plus de SMS du backend → je suis vraiment isolé ».

**Coordination requise avec le backend (cf nouveau INV-067 dans tests/INVARIANTS.md §6)** :

Dès qu'un opérateur d'astreinte (pos 1 chaîne) a un heartbeat HTTP > 30 s (≈ 1 cycle de watchdog INV-041 backend), le backend envoie immédiatement 1 SMS `[ALARME-MURGAT-PING] $timestamp` via la gateway SIM7600 au numéro du téléphone, et continue à en envoyer toutes les 2 min tant que le heartbeat reste KO. But : laisser au tél au moins 1 SMS qui arrive dans la fenêtre de 5 min après `heartbeatLostSince` côté client (marge de 2 min pour le routage SMSC).

**Permissions Android requises** :
- `RECEIVE_SMS` (runtime, dangerous) — demandée au login dans `MainActivity`. Si refusée, l'app ne peut pas confirmer la réception de pings → tombera systématiquement dans le cas sonnerie au moindre heartbeat KO. Onboarding doit expliquer cette dépendance.
- `BROADCAST_SMS` sur le receiver (manifest) — empêche un broadcast forgé par une app tierce malveillante.

**Format SMS attendu côté backend** :
```
[ALARME-MURGAT-PING] $iso_timestamp
```
Préfixe exact, pas de variation. Le timestamp est purement informatif (audit visible côté app Messages standard). Pas de signature en V1 — le préfixe seul est statistiquement suffisant pour éviter les faux positifs (un SMS tiers contenant cette chaîne exacte = probabilité quasi-nulle).

**Cas particuliers et coexistence métier** :
- **Tone Android sur réception SMS** : le tél d'astreinte fera son « ding » + vibration standard à chaque ping reçu (cf décision V1 du 2026-06-02). C'est même utile : feedback positif « le système me parle, tout va bien ». Setup manuel possible côté tél pour mute le contact gateway si trop intrusif (cf option (2) discutée le 2026-06-02).
- **SMS d'alarme métier (INV-060)** : ils ne matchent **pas** le préfixe `[ALARME-MURGAT-PING]`. Ils sont traités par leur propre logique (notif + sonnerie d'alarme normale, cf INV-ANDROID-001/002). Aucune interférence avec la mécanique INV-308.
- **Premier login de l'app** : `lastPingReceivedElapsedMs = elapsedRealtime()` au démarrage du service polling. Donne 5 min de grâce avant la 1re possibilité de sonnerie.
- **Backend en panne (gateway down)** : aucun ping ne sort → après 5 min de heartbeat KO, le tél sonne. Faux positif acceptable (signal correct « le système ne me parle plus »), absorbé par le snooze INV-307.

**Statut** : **🐛 spec à implémenter**. Le code mergé en PR #153 (sens inverse, "SMS WAKE on reception") a été **retiré** dans la même PR que cette spec (cf revert ci-après) — le code Android est revenu à l'état pré-#153, basé sur les heuristiques INV-302-LEGACY / INV-305-LEGACY. À implémenter à partir de cet état clean :
- Nouveau `SmsPingReceiver` (BroadcastReceiver static, manifest, perm `RECEIVE_SMS`) matche `[ALARME-MURGAT-PING]` et set `lastPingReceivedElapsedMs`.
- Nouvelle variable `lastPingReceivedElapsedMs: Long` dans `AlarmPollingService` (resettée sur heartbeat 2xx, cf INV-303).
- `AlarmPollingService.onHeartbeatFail()` : remplacer la consultation de `NetworkAvailabilityMonitor.isNoNetwork` (INV-302-LEGACY) par `lastPingReceivedElapsedMs < heartbeatLostSince` (INV-308). Timer de la fenêtre 5 min au lieu de 2 min.
- `DashboardActivity` : la branche bandeau existante (cas `INV302_SONNERIE` / `INV104_VISUAL_ONLY`) reste, mais les textes sont ajustés pour refléter le nouveau déclencheur (cf INV-104 révisée).
- `NetworkAvailabilityMonitor.kt` (singleton legacy INV-305) et son init dans `AlarmPollingService` : à supprimer dans la même PR que l'implémentation INV-308 (ne sera plus consulté nulle part).
- Catalogue : INV-302/305/306 (déjà ❌ SUPERSEDED dans cette PR doc) peuvent voir leurs corps LEGACY retirés une fois INV-308 implémenté + validé prod.

**Tests à créer** (PR d'implémentation) :
- `test_no_ping_after_heartbeat_lost_5min_arms_alarm` : forcer heartbeatLost depuis 5 min, aucun ping → `heartbeatLostAlarm == true`.
- `test_ping_received_within_5min_after_lost_does_not_arm_alarm` : heartbeat KO, simuler SMS `[ALARME-MURGAT-PING]` à T+3 min → bandeau visible mais `heartbeatLostAlarm == false`.
- `test_ping_received_before_heartbeat_lost_does_not_count` : SMS ping à T-1 min, puis heartbeat KO à T0, attendre 5 min sans nouveau ping → la sonnerie s'arme (les pings antérieurs à `heartbeatLostSince` ne comptent pas).
- `test_heartbeat_2xx_resets_everything` : armer, snoozer, heartbeat OK → tous les drapeaux à zéro.
- `test_snooze_quota_inchange` : snooze 3 fois → bouton GONE.

**Dépendances** :
- Backend : nouveau INV-067 [C] "envoi SMS ping pour pos 1 en heartbeat KO" (cf tests/INVARIANTS.md §6 ci-dessous). Sans cet invariant côté backend, l'app sonnera systématiquement au moindre heartbeat KO.
- Gateway SIM7600 : capable d'envoyer (cf INV-060/061/062 réutilisés). Aucune modif.
- INV-ANDROID-007 (canal sonore) : réutilisé tel quel.
- INV-ANDROID-104 (bandeau visuel) : réutilisé pour le cas "ping reçu, pas de sonnerie".
- INV-ANDROID-300 (heartbeat 3s) : inchangé, pilote la perte heartbeat.
- INV-ANDROID-303 (heartbeat 2xx reset) : étendu pour aussi reset `lastPingReceivedElapsedMs` et le snooze SMS.
- INV-ANDROID-307 (snooze 5 min × 3) : inchangé, applique sur la sonnerie de cette nouvelle INV-308.

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
- **Révision INV-079 (2026-06-15)** : `tryRefreshToken()` envoie maintenant le `refresh_token` (UUID opaque) dans le body de `POST /auth/refresh`, pas l'access expiré en header. Stocké dans `prefs.refresh_token` au login (cf INV-079). Le nouveau access reçu remplace l'ancien dans prefs+mémoire comme avant.

### INV-ANDROID-506 [C] ⚠️ Refresh échoué → sonnerie locale + message permanent, PAS logout silencieux (révisé 2026-06-15)
`forceLogout()` NE force PAS de retour à MainActivity. Au lieu : `authErrorMessage = "Votre session a expiré..."` puis `authErrorAlarm = true` puis `AppLogger.log("Auth", "ERREUR: $authErrorMessage")`. Bandeau permanent visible, sonnerie démarrée, bouton "Reconnexion" visible.
- **Pourquoi** : un logout silencieux = l'utilisateur manque des alarmes sans savoir pourquoi. Mieux vaut une alerte bruyante qui le force à agir.
- **Changement 2026-06-15 (INV-079)** : ce code path est désormais **très rare**. Le refresh token côté serveur (cf INV-079) est éternel sauf si révoqué — l'app peut rester éteinte des semaines et le refresh marchera au retour. `forceLogout()` ne se déclenche qu'en cas de révocation admin, suppression du user, ou backend indisponible > 1 cycle de refresh. Avant INV-079, la fenêtre était 24h (TTL JWT access).
- **Bug ordre log fixé (2026-06-15)** : avant, `AppLogger.log("Auth", "ERREUR: ${authErrorMessage}")` était appelé AVANT l'assignation de `authErrorMessage`, donc loguait toujours "ERREUR: null". L'ordre est maintenant : assignation → set flag → log avec le message correct.
- **Logs AppLogger ajoutés (INV-079)** : visibilité complète sur le path auth — `"Polling 401 → tentative refresh"`, `"Refresh OK"`, `"Refresh KO HTTP <code>"`, `"Refresh erreur reseau : <msg>"`, `"Refresh impossible : refresh_token absent"`. Le bouton "Envoyer les logs" expose maintenant exactement ce qui s'est passé sur l'auth.
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
