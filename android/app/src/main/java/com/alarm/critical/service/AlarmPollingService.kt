package com.alarm.critical.service

import android.app.*
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.util.Log
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
        const val EXTRA_TOKEN = "token"
        var isRunning = false
        var lastConnectionStatus = "Unknown"
        var lastHeartbeatOk = false
        var activeAlarmCount = 0
        var currentAlarm: com.alarm.critical.model.AlarmResponse? = null

        // Perte de heartbeat : alerte après ce délai (2 min par défaut, configurable pour tests)
        var heartbeatLossTimeoutMs = 120_000L
        var heartbeatLostSince: Long = 0L
        var heartbeatLostAlarm = false

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
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
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
                            Log.i(TAG, "Active alarm detected: ${alarms.first().title}")
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
                        // Heartbeat OK → reset du compteur de perte
                        heartbeatLostSince = 0L
                        heartbeatLostAlarm = false
                    } else if (response.code() == 503) {
                        // 503 = replica — signaler au poll de switcher
                        Log.w(TAG, "Heartbeat: backend is replica (503)")
                        needsUrlSwitch = true
                    } else {
                        onHeartbeatFail()
                    }
                } catch (e: Exception) {
                    onHeartbeatFail()
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

    private fun onHeartbeatFail() {
        lastHeartbeatOk = false
        val now = android.os.SystemClock.elapsedRealtime()
        if (heartbeatLostSince == 0L) {
            heartbeatLostSince = now
        }
        // Si la perte dure plus que le timeout → déclencher l'alerte
        val elapsed = now - heartbeatLostSince
        if (elapsed >= heartbeatLossTimeoutMs && !heartbeatLostAlarm) {
            heartbeatLostAlarm = true
            Log.w(TAG, "Heartbeat perdu depuis ${elapsed}ms — alerte connexion déclenchée")
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
        return try {
            val response = ApiProvider.service.refreshToken("Bearer $token")
            if (response.isSuccessful) {
                val newToken = response.body()?.access_token
                if (newToken != null) {
                    token = newToken
                    // Sauvegarder le nouveau token dans SharedPreferences
                    val prefs = getSharedPreferences("alarm_prefs", MODE_PRIVATE)
                    prefs.edit().putString("token", newToken).apply()
                    Log.i(TAG, "Token renouvelé avec succès")
                    true
                } else false
            } else {
                Log.w(TAG, "Refresh token échoué: ${response.code()}")
                false
            }
        } catch (e: Exception) {
            Log.e(TAG, "Refresh token erreur: ${e.message}")
            false
        }
    }

    private fun forceLogout() {
        // Ne PAS faire de logout silencieux — déclencher une alarme sonore continue
        // et afficher un message permanent compréhensible par un utilisateur lambda
        authErrorAlarm = true
        authErrorMessage = "Votre session a expiré et n'a pas pu être renouvelée. Veuillez vous reconnecter."
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
        startActivity(intent)
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
            .setContentTitle("Système d'alarme critique")
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
        super.onDestroy()
    }
}
