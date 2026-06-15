package com.alarm.critical.service

import android.app.*
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.util.Log
import androidx.core.app.NotificationCompat
import com.alarm.critical.AlarmActivity
import com.alarm.critical.api.ApiClient
import com.alarm.critical.api.ApiProvider
import kotlinx.coroutines.*

class AlarmPollingService : Service() {
    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private var pollingJob: Job? = null
    private var heartbeatJob: Job? = null
    private var tokenRefreshJob: Job? = null
    private var token: String? = null
    private val TAG = "AlarmPollingService"

    companion object {
        const val CHANNEL_ID = "alarm_polling_channel"
        const val NOTIFICATION_ID = 1
        const val ALARM_NOTIFICATION_ID = 999
        const val EXTRA_TOKEN = "token"
        var isRunning = false
        var lastConnectionStatus = "Unknown"
        var lastHeartbeatOk = false
        var activeAlarmCount = 0
        var currentAlarm: com.alarm.critical.model.AlarmResponse? = null

        // Perte de heartbeat : alerte après ce délai (2 min par défaut, configurable pour tests)
        var heartbeatLossTimeoutMs = 120_000L
        var heartbeatLostSince: Long = 0L

        // INV-ANDROID-104 diag : dernière cause d'échec heartbeat ("HTTP 401",
        // "HTTP 500", "reseau KO (...)"). Le heartbeat tape toutes les 3s ; logger
        // chaque échec saturerait le buffer AppLogger (500 entrées) en ~25 min.
        // On ne loge donc que sur transition (1er échec, passage des 120s) ET sur
        // changement de cause — d'où cette variable qui mémorise la dernière cause.
        var lastHeartbeatFailReason: String? = null

        // INV-ANDROID-104 (2026-05-26) : 2 drapeaux distincts depuis le découplage
        // bandeau visuel / sonnerie locale.
        //
        // `heartbeatLostVisual` (info) : armé dès que `heartbeatLostSince > timeout`.
        //   → bandeau visible (texte rassurant "Serveur injoignable — SMS dispo").
        //   → AUCUNE sonnerie.
        //
        // `heartbeatLostAlarm` (action requise) : armé quand `heartbeatLostVisual`
        //   ET `NetworkAvailabilityMonitor.isNoNetwork` (cf INV-ANDROID-305).
        //   → bandeau visible (texte d'action "déplacez-vous pour retrouver du
        //     réseau") + sonnerie locale via `connectionLostSoundManager`.
        //
        // Le drapeau sonnerie tombe à `false` dès qu'un des canaux réseau revient
        // (même si heartbeat encore perdu) — le téléphone est à nouveau joignable
        // par le système d'astreinte (SMS gateway / push différé).
        var heartbeatLostVisual = false
        var heartbeatLostAlarm = false

        // INV-ANDROID-307 (2026-05-29) : snooze 5 min de la sonnerie locale.
        // Pendant le snooze, la sonnerie est désactivée même si les conditions
        // (heartbeat lost + noNetwork) restent vraies. Limité à 3 occurrences
        // par épisode pour éviter qu'un opérateur d'astreinte ne se mette
        // silencieux à perpétuité. L'épisode se termine quand le heartbeat
        // revient OK (reset des compteurs).
        const val LOCAL_ALARM_SNOOZE_DURATION_MS = 5 * 60 * 1000L
        const val LOCAL_ALARM_SNOOZE_MAX_COUNT = 3
        var snoozeUntilElapsedMs: Long = 0L
        var snoozeCount: Int = 0

        /** True si une sonnerie locale est actuellement en cours de snooze. */
        fun isLocalAlarmSnoozed(
            now: Long = android.os.SystemClock.elapsedRealtime()
        ): Boolean {
            return snoozeUntilElapsedMs > 0L && now < snoozeUntilElapsedMs
        }

        /** Snooze restant en millisecondes (0 si pas en snooze). */
        fun snoozeRemainingMs(
            now: Long = android.os.SystemClock.elapsedRealtime()
        ): Long {
            return (snoozeUntilElapsedMs - now).coerceAtLeast(0L)
        }

        /**
         * INV-ANDROID-307 : déclenche un snooze de 5 min sur la sonnerie locale.
         * Retourne `true` si snooze armé, `false` si quota épuisé
         * (`snoozeCount >= LOCAL_ALARM_SNOOZE_MAX_COUNT`).
         */
        fun snoozeLocalAlarm(): Boolean {
            if (snoozeCount >= LOCAL_ALARM_SNOOZE_MAX_COUNT) {
                com.alarm.critical.util.AppLogger.log(
                    "Heartbeat",
                    "Snooze refuse : quota epuise " +
                        "($snoozeCount/$LOCAL_ALARM_SNOOZE_MAX_COUNT)"
                )
                return false
            }
            snoozeCount += 1
            snoozeUntilElapsedMs =
                android.os.SystemClock.elapsedRealtime() + LOCAL_ALARM_SNOOZE_DURATION_MS
            heartbeatLostAlarm = false
            com.alarm.critical.util.AppLogger.log(
                "Heartbeat",
                "Sonnerie INV-302 mise en sourdine 5 min " +
                    "(snooze $snoozeCount/$LOCAL_ALARM_SNOOZE_MAX_COUNT)"
            )
            return true
        }

        // Refresh token toutes les 12h (configurable pour tests)
        var tokenRefreshIntervalMs = 12 * 60 * 60 * 1000L

        // Erreur d'authentification irréversible (refresh échoué)
        var authErrorAlarm = false
        var authErrorMessage: String? = null

        // Flag : le heartbeat a recu 503 (replica), le poll doit switcher
        @Volatile
        var needsUrlSwitch = false

        // Flag : service demarre par un push FCM (mode veille)
        var startedByFcm = false

        // ID de l'alarme pour laquelle on a déjà lancé l'AlarmActivity
        var alarmActivityLaunchedForId: Int = -1
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
        // INV-ANDROID-305 : démarrer la surveillance réseau (data + cellulaire)
        // pour piloter l'armement de la sonnerie hors-connexion (INV-302).
        com.alarm.critical.util.NetworkAvailabilityMonitor.init(this)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        token = intent?.getStringExtra(EXTRA_TOKEN)
        if (token == null) {
            stopSelf()
            return START_NOT_STICKY
        }

        // Detecter si demarre par FCM (mode veille)
        if (intent?.getBooleanExtra("started_by_fcm", false) == true) {
            startedByFcm = true
        }

        val notification = buildNotification("Surveillance des alarmes...")
        startForeground(NOTIFICATION_ID, notification)
        isRunning = true
        com.alarm.critical.util.AppLogger.log("Service", "Polling demarre (fcm=${startedByFcm})")

        // Trouver le primary avant de demarrer le polling
        scope.launch {
            findPrimaryUrl()
            startPolling()
            startHeartbeat()
            startTokenRefresh()
        }

        return START_STICKY
    }

    /**
     * Probe chaque URL backend pour trouver le primary (heartbeat 200).
     * Si l'URL courante est un replica (503), bascule immédiatement.
     */
    private suspend fun findPrimaryUrl() {
        try {
            val response = ApiProvider.service.heartbeat("Bearer $token")
            if (response.isSuccessful) {
                Log.i(TAG, "URL courante est le primary")
                return
            }
        } catch (_: Exception) {}

        // L'URL courante ne marche pas — essayer les autres
        Log.w(TAG, "URL courante n'est pas le primary, recherche...")
        for (i in 0 until 3) {
            ApiClient.switchToNextUrl()
            try {
                val response = ApiProvider.service.heartbeat("Bearer $token")
                if (response.isSuccessful) {
                    Log.i(TAG, "Primary trouvé sur URL index ${ApiClient.currentUrlIndex}")
                    return
                }
            } catch (_: Exception) {}
        }
        Log.e(TAG, "Aucun primary trouvé, on demarre avec l'URL courante")
    }

    private fun startPolling() {
        pollingJob?.cancel()
        pollingJob = scope.launch {
            while (isActive) {
                try {
                    val response = ApiProvider.service.getMyAlarms("Bearer $token")
                    if (response.isSuccessful) {
                        // Le heartbeat a signale qu'on est sur un replica → switcher
                        if (needsUrlSwitch) {
                            needsUrlSwitch = false
                            Log.w(TAG, "Poll: heartbeat signale replica, switching URL")
                            ApiClient.switchToNextUrl()
                            // Attendre un cycle heartbeat complet pour que le heartbeat
                            // teste la nouvelle URL avant de re-verifier le flag
                            delay(4000)
                            continue
                        }
                        ApiClient.consecutiveFailures = 0
                        lastConnectionStatus = "Connected"
                        // Clear auth error si la connexion remarche
                        if (authErrorAlarm) {
                            authErrorAlarm = false
                            authErrorMessage = null
                        }
                        val alarms = response.body() ?: emptyList()
                        activeAlarmCount = alarms.size
                        currentAlarm = alarms.firstOrNull()

                        if (alarms.isNotEmpty()) {
                            val alarm = alarms.first()
                            Log.i(TAG, "Active alarm detected: ${alarm.title}")
                            com.alarm.critical.util.AppLogger.log("Alarme", "Detectee: ${alarm.title} (id=${alarm.id}, status=${alarm.status})")

                            // Lancer l'AlarmActivity via full-screen notification (une seule fois par alarme)
                            if (alarm.id != alarmActivityLaunchedForId && !AlarmActivity.isVisible) {
                                alarmActivityLaunchedForId = alarm.id
                                Log.w(TAG, "Launching AlarmActivity from service for alarm #${alarm.id}")
                                launchAlarmActivity(
                                    alarmId = alarm.id,
                                    title = alarm.title,
                                    message = alarm.message,
                                    severity = alarm.severity
                                )
                            }
                        } else {
                            // Plus d'alarme active → reset du flag
                            alarmActivityLaunchedForId = -1
                        }

                        updateNotification("Surveillance - ${alarms.size} alarme(s) active(s)")

                        // Mode veille : arreter le service quand plus d'alarme active
                        if (alarms.isEmpty() && startedByFcm) {
                            val prefs = getSharedPreferences("alarm_prefs", MODE_PRIVATE)
                            val isOncall = prefs.getBoolean("is_oncall", true)
                            if (!isOncall) {
                                Log.w(TAG, "Alarm resolved + mode veille → stopping service")
                                prefs.edit().putBoolean("started_by_fcm", false).apply()
                                stopSelf()
                                return@launch
                            }
                        }
                    } else if (response.code() == 401) {
                        Log.w(TAG, "Token expiré (401) — tentative de refresh")
                        com.alarm.critical.util.AppLogger.log(
                            "Auth",
                            "Polling 401 → tentative refresh via /auth/refresh (INV-079)"
                        )
                        if (!tryRefreshToken()) {
                            Log.e(TAG, "Refresh échoué — déconnexion forcée")
                            forceLogout()
                            return@launch
                        }
                    } else {
                        onPollFailure("Error: ${response.code()}")
                    }
                } catch (e: Exception) {
                    onPollFailure("Disconnected: ${e.message}")
                }
                delay(3000) // Poll every 3 seconds
            }
        }
    }

    private fun startHeartbeat() {
        heartbeatJob?.cancel()
        heartbeatJob = scope.launch {
            while (isActive) {
                try {
                    val response = ApiProvider.service.heartbeat("Bearer $token")
                    if (response.isSuccessful) {
                        lastHeartbeatOk = true
                        needsUrlSwitch = false  // On est sur le primary, plus besoin de switch
                        lastHeartbeatFailReason = null
                        val wasLost = heartbeatLostSince != 0L
                        val wasInAlarm = heartbeatLostAlarm
                        val wasInSnooze = isLocalAlarmSnoozed()
                        // Heartbeat OK → reset bandeau ET sonnerie (cf INV-ANDROID-303)
                        heartbeatLostSince = 0L
                        heartbeatLostVisual = false
                        heartbeatLostAlarm = false
                        // INV-ANDROID-307 : fin d'episode → reset le quota de snooze
                        snoozeUntilElapsedMs = 0L
                        snoozeCount = 0
                        if (wasLost || wasInAlarm || wasInSnooze) {
                            com.alarm.critical.util.AppLogger.log(
                                "Heartbeat",
                                "Heartbeat 2xx → reset complet " +
                                    "(wasLost=$wasLost wasInAlarm=$wasInAlarm wasInSnooze=$wasInSnooze) " +
                                    "tous drapeaux + snoozeCount remis a zero (INV-303 + INV-307)"
                            )
                        }
                    } else if (response.code() == 503) {
                        // 503 = replica — signaler au poll de switcher (comportement inchangé :
                        // on n'arme PAS heartbeatLostSince, le poll switch d'URL en ~4s).
                        Log.w(TAG, "Heartbeat: backend is replica (503)")
                        if (!needsUrlSwitch) {
                            com.alarm.critical.util.AppLogger.log(
                                "Heartbeat",
                                "HTTP 503 (replica) → switch URL demande au poll (INV-ANDROID-304)"
                            )
                        }
                        needsUrlSwitch = true
                    } else {
                        onHeartbeatFail("HTTP ${response.code()}")
                    }
                } catch (e: Exception) {
                    onHeartbeatFail("reseau KO (${e.message})")
                }
                delay(3000)
            }
        }
    }

    /**
     * Appelée sur chaque échec de polling (non-401).
     * Incrémente le compteur et déclenche un failover après 3 échecs consécutifs.
     */
    private fun onPollFailure(reason: String) {
        lastConnectionStatus = reason
        Log.w(TAG, "Poll failure: $reason")
        ApiClient.consecutiveFailures++
        if (ApiClient.consecutiveFailures >= 3) {
            Log.w(TAG, "3 échecs consécutifs — bascule vers l'URL secondaire")
            ApiClient.switchToNextUrl()
        }
    }

    private fun onHeartbeatFail(reason: String = "inconnu") {
        lastHeartbeatOk = false
        val now = android.os.SystemClock.elapsedRealtime()
        val wasZero = heartbeatLostSince == 0L
        if (wasZero) {
            heartbeatLostSince = now
            // INV-ANDROID-306 : premier echec → log explicite pour pouvoir
            // mesurer le delai d'apparition de l'ArcTimer dans le bandeau.
            val noNetworkInit = com.alarm.critical.util.NetworkAvailabilityMonitor.isNoNetwork
            com.alarm.critical.util.AppLogger.log(
                "Heartbeat",
                "1er echec → heartbeatLostSince arme. cause=$reason " +
                    "data=${com.alarm.critical.util.NetworkAvailabilityMonitor.dataAvailable} " +
                    "cell=${com.alarm.critical.util.NetworkAvailabilityMonitor.cellularInService} " +
                    "isNoNetwork=$noNetworkInit timeout=${heartbeatLossTimeoutMs}ms " +
                    "→ ArcTimer INV-306 devrait s'afficher au tick UI suivant si isNoNetwork=true"
            )
        } else if (reason != lastHeartbeatFailReason) {
            // La cause de l'echec a change pendant l'episode (ex: HTTP 401 → reseau KO,
            // ou 500 → 401). Loguer la transition pour suivre l'evolution sans flooder
            // le buffer (un seul log par changement de cause, pas a chaque tick 3s).
            com.alarm.critical.util.AppLogger.log(
                "Heartbeat",
                "echec continue, cause change: ${lastHeartbeatFailReason ?: "?"} → $reason " +
                    "(perdu depuis ${(now - heartbeatLostSince) / 1000}s)"
            )
        }
        lastHeartbeatFailReason = reason
        val elapsed = now - heartbeatLostSince
        if (elapsed >= heartbeatLossTimeoutMs) {
            // INV-ANDROID-104 : bandeau visuel armé d'office (info "serveur injoignable")
            if (!heartbeatLostVisual) {
                heartbeatLostVisual = true
                com.alarm.critical.util.AppLogger.log(
                    "Heartbeat",
                    "PERDU depuis ${elapsed/1000}s (>= timeout ${heartbeatLossTimeoutMs/1000}s) — " +
                        "cause=$reason — INV-104 bandeau visuel arme"
                )
                Log.w(TAG, "Heartbeat perdu depuis ${elapsed}ms — bandeau visuel déclenché")
            }
            // INV-ANDROID-302 : sonnerie locale armée UNIQUEMENT si pas de reseau du tout.
            // INV-ANDROID-307 : en periode de snooze, on ne (re-)arme PAS la sonnerie ;
            // a la fin du snooze, le tick suivant l'armera si les conditions sont encore vraies.
            val noNetwork = com.alarm.critical.util.NetworkAvailabilityMonitor.isNoNetwork
            val snoozed = isLocalAlarmSnoozed(now)
            if (noNetwork && !snoozed && !heartbeatLostAlarm) {
                heartbeatLostAlarm = true
                com.alarm.critical.util.AppLogger.log(
                    "Heartbeat",
                    "INV-302 sonnerie armee (noNetwork=true snoozeCount=$snoozeCount " +
                        "data=${com.alarm.critical.util.NetworkAvailabilityMonitor.dataAvailable} " +
                        "cell=${com.alarm.critical.util.NetworkAvailabilityMonitor.cellularInService})"
                )
                Log.w(TAG, "Reseau totalement perdu — sonnerie locale armée (INV-302)")
            } else if (!noNetwork && heartbeatLostAlarm) {
                // Reseau revenu (data OU cellular) — couper la sonnerie même si heartbeat encore perdu
                heartbeatLostAlarm = false
                com.alarm.critical.util.AppLogger.log(
                    "Heartbeat",
                    "INV-302 sonnerie desarmee (reseau revenu " +
                        "data=${com.alarm.critical.util.NetworkAvailabilityMonitor.dataAvailable} " +
                        "cell=${com.alarm.critical.util.NetworkAvailabilityMonitor.cellularInService})"
                )
                Log.w(TAG, "Reseau revenu — sonnerie locale désarmée")
            } else if (noNetwork && snoozed && !heartbeatLostAlarm) {
                // Pas de log spammeur, mais utile une fois en debug
                // (snoozeRemaining decroit a chaque tick — eviter le spam : ne log pas ici)
            }
        }
    }

    private fun startTokenRefresh() {
        tokenRefreshJob?.cancel()
        tokenRefreshJob = scope.launch {
            while (isActive) {
                delay(tokenRefreshIntervalMs)
                Log.i(TAG, "Renouvellement automatique du token...")
                tryRefreshToken()
            }
        }
    }

    private suspend fun tryRefreshToken(): Boolean {
        val prefs = getSharedPreferences("alarm_prefs", MODE_PRIVATE)
        // INV-079 : lire le refresh_token persiste au login. Si absent (cas
        // legacy : login pre-INV-079 ou apres un logout/clear), refresh
        // impossible → retourne false → forceLogout() prendra le relais.
        val refresh = prefs.getString("refresh_token", null)
        if (refresh.isNullOrEmpty()) {
            Log.w(TAG, "Refresh impossible : aucun refresh_token stocke (login pre-INV-079 ?)")
            com.alarm.critical.util.AppLogger.log(
                "Auth",
                "Refresh impossible : refresh_token absent en SharedPreferences (INV-079)"
            )
            return false
        }
        return try {
            val response = ApiProvider.service.refreshToken(
                com.alarm.critical.model.RefreshRequest(refresh_token = refresh)
            )
            if (response.isSuccessful) {
                val newToken = response.body()?.access_token
                if (newToken != null) {
                    token = newToken
                    prefs.edit().putString("token", newToken).apply()
                    Log.i(TAG, "Token renouvelé avec succès")
                    com.alarm.critical.util.AppLogger.log(
                        "Auth",
                        "Refresh OK (INV-079) : nouveau access token stocke"
                    )
                    true
                } else {
                    com.alarm.critical.util.AppLogger.log(
                        "Auth",
                        "Refresh 2xx mais body sans access_token (incoherence backend)"
                    )
                    false
                }
            } else {
                Log.w(TAG, "Refresh token échoué: ${response.code()}")
                com.alarm.critical.util.AppLogger.log(
                    "Auth",
                    "Refresh KO HTTP ${response.code()} : refresh_token rejeté par /auth/refresh (revoque, expire, ou backend pre-INV-079)"
                )
                false
            }
        } catch (e: Exception) {
            Log.e(TAG, "Refresh token erreur: ${e.message}")
            com.alarm.critical.util.AppLogger.log(
                "Auth",
                "Refresh erreur reseau : ${e.message}"
            )
            false
        }
    }

    private fun forceLogout() {
        // Ne PAS faire de logout silencieux — déclencher une alarme sonore continue
        // et afficher un message permanent compréhensible par un utilisateur lambda.
        // INV-079 : ce code path est très rare avec le refresh éternel — il ne
        // se déclenche que sur revocation admin ou backend down très longtemps.
        authErrorMessage = "Votre session a expiré et n'a pas pu être renouvelée. Veuillez vous reconnecter."
        authErrorAlarm = true
        // Le log se fait APRES l'assignation du message (bug ordre fixe 2026-06-15).
        com.alarm.critical.util.AppLogger.log("Auth", "ERREUR: $authErrorMessage")
        Log.e(TAG, "Échec du renouvellement de session — sonnerie d'alerte déclenchée")
    }

    private fun launchAlarmActivity(alarmId: Int, title: String, message: String, severity: String) {
        val intent = Intent(this, AlarmActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
            putExtra("alarm_id", alarmId)
            putExtra("alarm_title", title)
            putExtra("alarm_message", message)
            putExtra("alarm_severity", severity)
            putExtra("token", token)
        }

        // Full-screen intent notification — le mécanisme officiel pour afficher
        // une activité urgente même quand l'app est en arrière-plan (Android 10+)
        val fullScreenPendingIntent = PendingIntent.getActivity(
            this, alarmId, intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val alarmChannelId = "alarm_critical_channel"
        val channel = NotificationChannel(
            alarmChannelId,
            "Alarmes critiques",
            NotificationManager.IMPORTANCE_HIGH
        ).apply {
            description = "Notifications d'alarmes critiques"
            setBypassDnd(true)
            lockscreenVisibility = Notification.VISIBILITY_PUBLIC
        }
        val manager = getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(channel)

        val notification = NotificationCompat.Builder(this, alarmChannelId)
            .setSmallIcon(android.R.drawable.ic_dialog_alert)
            .setContentTitle("ALARME CRITIQUE")
            .setContentText(title)
            .setPriority(NotificationCompat.PRIORITY_MAX)
            .setCategory(NotificationCompat.CATEGORY_ALARM)
            .setFullScreenIntent(fullScreenPendingIntent, true)
            .setAutoCancel(true)
            .build()

        manager.notify(ALARM_NOTIFICATION_ID, notification)
        Log.w(TAG, "Full-screen alarm notification posted for alarm #$alarmId")
    }

    private fun createNotificationChannel() {
        val channel = NotificationChannel(
            CHANNEL_ID,
            "Surveillance des alarmes",
            NotificationManager.IMPORTANCE_LOW
        ).apply {
            description = "Surveillance en arrière-plan des alarmes critiques"
        }
        val manager = getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(channel)
    }

    private fun buildNotification(text: String): Notification {
        return Notification.Builder(this, CHANNEL_ID)
            .setContentTitle("Alarme Murgat")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.ic_dialog_alert)
            .setOngoing(true)
            .build()
    }

    private fun updateNotification(text: String) {
        val notification = buildNotification(text)
        val manager = getSystemService(NotificationManager::class.java)
        manager.notify(NOTIFICATION_ID, notification)
    }

    override fun onDestroy() {
        isRunning = false
        pollingJob?.cancel()
        heartbeatJob?.cancel()
        tokenRefreshJob?.cancel()
        scope.cancel()
        // INV-ANDROID-305 : libérer les callbacks réseau
        com.alarm.critical.util.NetworkAvailabilityMonitor.release()
        super.onDestroy()
    }
}
