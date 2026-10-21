package com.alarm.critical.service

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Intent
import android.util.Log
import androidx.core.app.NotificationCompat
import com.alarm.critical.MainActivity
import com.alarm.critical.R
import com.alarm.critical.api.ApiClient
import com.alarm.critical.api.ApiProvider
import com.alarm.critical.model.FcmTokenRequest
import com.google.firebase.messaging.FirebaseMessagingService
import com.google.firebase.messaging.RemoteMessage
import kotlinx.coroutines.*

/**
 * Service Firebase qui recoit les push notifications FCM.
 * Delegue le traitement a AlarmWakeUpHandler (testable en isolation).
 */
class AlarmFirebaseService : FirebaseMessagingService() {
    companion object {
        private const val TAG = "AlarmFCM"
        private const val ESCALATION_CHANNEL_ID = "escalation_channel"
        private const val ESCALATION_NOTIFICATION_ID = 3001
    }

    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())

    override fun onMessageReceived(message: RemoteMessage) {
        super.onMessageReceived(message)
        Log.w(TAG, "FCM message received: ${message.data}")

        val data = message.data
        val msgType = data["type"]

        if (msgType == "escalation_update") {
            // Mise a jour de la position dans la chaine d'escalade
            val position = data["escalation_position"]?.toIntOrNull() ?: -1
            val isOncall = data["is_oncall"] == "true"
            AlarmWakeUpHandler.onEscalationPushReceived(applicationContext, position, isOncall)
            showEscalationNotification(data["title"], data["body"])
            return
        }

        if (msgType == "alarm_resolved") {
            AlarmWakeUpHandler.onAlarmResolvedPushReceived(applicationContext, data["alarm_id"])
            // Notif visible : sinon FCM degrade les push haute priorite suivants.
            showEscalationNotification(data["title"], data["body"])
            return
        }

        val alarmId = data["alarm_id"]
        val title = data["title"] ?: message.notification?.title
        val severity = data["severity"]

        if (alarmId != null) {
            AlarmWakeUpHandler.onAlarmPushReceived(
                context = applicationContext,
                alarmId = alarmId,
                title = title,
                severity = severity
            )
        } else {
            Log.d(TAG, "FCM message without alarm_id, ignored")
        }
    }

    /**
     * Push data-only : c'est a l'app d'afficher la notif. Obligatoire aussi pour
     * FCM : un push haute priorite sans notif visible fait degrader les suivants
     * en priorite normale (retardes par Doze).
     */
    private fun showEscalationNotification(title: String?, body: String?) {
        val manager = getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(
            NotificationChannel(ESCALATION_CHANNEL_ID, "Chaine d'escalade", NotificationManager.IMPORTANCE_DEFAULT)
        )
        val launch = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        val notification = NotificationCompat.Builder(this, ESCALATION_CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_stat_alarm)
            .setContentTitle(title ?: "Chaine d'escalade modifiee")
            .setContentText(body)
            .setContentIntent(launch)
            .setAutoCancel(true)
            .build()
        manager.notify(ESCALATION_NOTIFICATION_ID, notification)
    }

    override fun onNewToken(token: String) {
        super.onNewToken(token)
        Log.w(TAG, "New FCM token: ${token.take(20)}...")

        // Envoyer le nouveau token au backend
        val prefs = getSharedPreferences("alarm_prefs", MODE_PRIVATE)
        val authToken = prefs.getString("token", null) ?: return
        val deviceId = prefs.getString("device_token", null) ?: return

        scope.launch {
            try {
                ApiProvider.service.registerFcmToken(
                    "Bearer $authToken",
                    FcmTokenRequest(token = token, device_id = deviceId)
                )
                Log.d(TAG, "FCM token registered on backend")
            } catch (e: Exception) {
                Log.e(TAG, "Failed to register FCM token: ${e.message}")
            }
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        scope.cancel()
    }
}
