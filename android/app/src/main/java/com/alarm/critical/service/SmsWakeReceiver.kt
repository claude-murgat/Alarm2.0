package com.alarm.critical.service

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.provider.Telephony
import com.alarm.critical.util.AppLogger
import com.alarm.critical.util.SmsWakeAlarmController

/**
 * INV-ANDROID-308 (2026-06-02) — réception de la commande "sonnerie hors connexion"
 * envoyée par le backend via SMS.
 *
 * Le récepteur est déclaré dans le manifest, donc actif **même si l'app est
 * killée** par l'OS / OEM. Idéal pour un tél d'astreinte où le foreground
 * service peut être tué après plusieurs heures sans interaction.
 *
 * Format attendu : préfixe `[ALARME-MURGAT-WAKE]` au début du payload SMS.
 * Le contenu après est libre (audit visible côté app Messages — sert de trace).
 *
 * Note V1 : pas de signature/HMAC. Le préfixe seul est statistiquement
 * suffisant pour éviter les faux positifs (probabilité quasi-nulle qu'un SMS
 * tiers contienne cette chaîne exacte). Pour la sécurité face à un acteur
 * malveillant connaissant le préfixe + le numéro de l'opérateur, V2 ajoutera
 * un token signé HMAC partagé via SharedPreferences au login.
 */
class SmsWakeReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Telephony.Sms.Intents.SMS_RECEIVED_ACTION) return

        val messages = try {
            Telephony.Sms.Intents.getMessagesFromIntent(intent)
        } catch (e: Exception) {
            AppLogger.log("SmsWake", "getMessagesFromIntent error: ${e.message}")
            return
        } ?: return

        // Les SMS multi-parts arrivent comme un tableau de fragments. On
        // concatène les body pour matcher le préfixe même s'il est coupé.
        val fullBody = messages.joinToString(separator = "") { it.messageBody ?: "" }
        val originator = messages.firstOrNull()?.originatingAddress ?: "?"

        if (!fullBody.startsWith(WAKE_PREFIX)) {
            // Ne pas logger les SMS standards (privacy)
            return
        }

        AppLogger.log(
            "SmsWake",
            "SMS wake recu de '$originator' (longueur ${fullBody.length} chars) — declenchement INV-308"
        )

        try {
            SmsWakeAlarmController.trigger(context)
        } catch (e: Exception) {
            AppLogger.log("SmsWake", "trigger error: ${e.message}")
        }
    }

    companion object {
        /**
         * Préfixe magique. Doit matcher exactement celui que le backend met
         * dans les SMS de commande envoyés via la gateway SIM7600.
         */
        const val WAKE_PREFIX = "[ALARME-MURGAT-WAKE]"
    }
}
