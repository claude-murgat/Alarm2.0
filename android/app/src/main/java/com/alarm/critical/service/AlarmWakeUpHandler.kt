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

    /**
     * Appele quand un push FCM `escalation_update` arrive (INV-ANDROID-601).
     *
     * Met a jour le cache local ET reconcilie le heartbeat. Sans la seconde
     * partie, la nouvelle position restait dans les prefs sans que personne ne
     * l'applique : `DashboardActivity` ne demarre le service que dans son
     * `onCreate`, donc un user promu en position 1 restait sans heartbeat
     * jusqu'a une relance de l'app, et le serveur le croyait hors ligne.
     */
    fun onEscalationPushReceived(context: Context, position: Int, isOncall: Boolean) {
        val prefs = context.getSharedPreferences("alarm_prefs", Context.MODE_PRIVATE)
        prefs.edit()
            .putInt("escalation_position", position)
            .putBoolean("is_oncall", isOncall)
            .apply()
        Log.w(TAG, "Escalation push: position=$position, is_oncall=$isOncall")

        if (isOncall) {
            val token = prefs.getString("token", null)
            if (token == null) {
                Log.w(TAG, "Escalation push: no saved token, cannot start heartbeat")
                return
            }
            // Meme garde qu'INV-ANDROID-602 : ne pas redemarrer un service sain.
            if (!AlarmPollingService.isRunning) {
                val intent = Intent(context, AlarmPollingService::class.java).apply {
                    putExtra(AlarmPollingService.EXTRA_TOKEN, token)
                }
                ContextCompat.startForegroundService(context, intent)
                Log.w(TAG, "Heartbeat started: now on call at position $position")
            }
        } else if (!prefs.getBoolean("started_by_fcm", false)) {
            // Plus de garde -> on coupe le heartbeat. Exception : si le service a
            // ete demarre par un push d'ALARME (started_by_fcm), il doit survivre
            // jusqu'a l'acquittement, sinon on fait taire une alarme en cours.
            // Commande d'arret plutot que stopService() : un stopService() juste apres
            // un startForegroundService() (ouverture d'app puis retrogradation) fait
            // planter l'app (startForeground() jamais appele).
            if (AlarmPollingService.isRunning) {
                ContextCompat.startForegroundService(context,
                    Intent(context, AlarmPollingService::class.java).setAction(AlarmPollingService.ACTION_STOP))
            }
            Log.w(TAG, "Heartbeat stopped: no longer on call (position $position)")
        }
    }
}
