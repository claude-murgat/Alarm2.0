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
import kotlinx.coroutines.*

class AlarmPollingService : Service() {
    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private var pollingJob: Job? = null
    private var heartbeatJob: Job? = null
    private var token: String? = null
    private val TAG = "AlarmPollingService"

    companion object {
        const val CHANNEL_ID = "alarm_polling_channel"
        const val NOTIFICATION_ID = 1
        const val EXTRA_TOKEN = "token"
        var isRunning = false
        var lastConnectionStatus = "Unknown"
        var activeAlarmCount = 0
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

        val notification = buildNotification("Monitoring for alarms...")
        startForeground(NOTIFICATION_ID, notification)
        isRunning = true

        startPolling()
        startHeartbeat()

        return START_STICKY
    }

    private fun startPolling() {
        pollingJob?.cancel()
        pollingJob = scope.launch {
            while (isActive) {
                try {
                    val response = ApiClient.service.getMyAlarms("Bearer $token")
                    if (response.isSuccessful) {
                        lastConnectionStatus = "Connected"
                        val alarms = response.body() ?: emptyList()
                        activeAlarmCount = alarms.size

                        if (alarms.isNotEmpty()) {
                            val alarm = alarms.first()
                            Log.i(TAG, "Active alarm detected: ${alarm.title}")
                            launchAlarmActivity(alarm.id, alarm.title, alarm.message, alarm.severity)
                        }

                        updateNotification("Monitoring - ${alarms.size} active alarm(s)")
                    } else {
                        lastConnectionStatus = "Error: ${response.code()}"
                        Log.w(TAG, "Poll failed: ${response.code()}")
                    }
                } catch (e: Exception) {
                    lastConnectionStatus = "Disconnected"
                    Log.e(TAG, "Poll error: ${e.message}")
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
                    ApiClient.service.heartbeat("Bearer $token")
                } catch (e: Exception) {
                    Log.e(TAG, "Heartbeat error: ${e.message}")
                }
                delay(30000) // Every 30 seconds
            }
        }
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
            "Alarm Monitoring",
            NotificationManager.IMPORTANCE_LOW
        ).apply {
            description = "Background monitoring for critical alarms"
        }
        val manager = getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(channel)
    }

    private fun buildNotification(text: String): Notification {
        return Notification.Builder(this, CHANNEL_ID)
            .setContentTitle("Critical Alarm System")
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
        scope.cancel()
        super.onDestroy()
    }
}
