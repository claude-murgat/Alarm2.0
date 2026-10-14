package com.alarm.critical.service

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/**
 * INV-ANDROID-704 : apres un redemarrage du telephone (ou une mise a jour de
 * l'app), relance le heartbeat si l'utilisateur est d'astreinte. Meme chemin
 * qu'un push d'escalade : prefs + reconciliation du service.
 */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED &&
            intent.action != Intent.ACTION_MY_PACKAGE_REPLACED) return
        val prefs = context.getSharedPreferences("alarm_prefs", Context.MODE_PRIVATE)
        AlarmWakeUpHandler.onEscalationPushReceived(
            context,
            prefs.getInt("escalation_position", -1),
            prefs.getBoolean("is_oncall", false)
        )
    }
}
