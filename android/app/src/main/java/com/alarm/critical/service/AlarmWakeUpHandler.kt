package com.alarm.critical.service

import android.content.Context
import android.content.Intent
import android.util.Log
import androidx.core.content.ContextCompat

/**
 * Handler testable qui reveille le foreground service quand un push FCM arrive.
 * Utilise par AlarmFirebaseService et testable en isolation (pas de dependance Firebase).
 */
object AlarmWakeUpHandler {
    private const val TAG = "AlarmWakeUpHandler"

    /**
     * Appele quand un push FCM d'alarme arrive.
     * Demarre le foreground service avec le token sauvegarde.
     */
    fun onAlarmPushReceived(context: Context, alarmId: String?, title: String?, severity: String?) {
        Log.w(TAG, "FCM alarm push received: id=$alarmId, title=$title, severity=$severity")

        val prefs = context.getSharedPreferences("alarm_prefs", Context.MODE_PRIVATE)
        val token = prefs.getString("token", null)

        if (token == null) {
            Log.w(TAG, "No saved token, cannot start service")
            return
        }

        // Marquer que le service est demarre par FCM (pas par login)
        prefs.edit().putBoolean("started_by_fcm", true).apply()

        if (!AlarmPollingService.isRunning) {
            val intent = Intent(context, AlarmPollingService::class.java).apply {
                putExtra(AlarmPollingService.EXTRA_TOKEN, token)
                putExtra("started_by_fcm", true)
            }
            ContextCompat.startForegroundService(context, intent)
            Log.w(TAG, "Foreground service started by FCM push")
        } else {
            Log.d(TAG, "Service already running, FCM push ignored")
        }
    }
}
