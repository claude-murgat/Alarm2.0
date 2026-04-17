package com.alarm.critical.service

import android.util.Log
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
            val prefs = getSharedPreferences("alarm_prefs", MODE_PRIVATE)
            prefs.edit()
                .putInt("escalation_position", position)
                .putBoolean("is_oncall", isOncall)
                .apply()
            Log.w(TAG, "Escalation updated: position=$position, is_oncall=$isOncall")
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
