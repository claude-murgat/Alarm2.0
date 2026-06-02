package com.alarm.critical.util

import android.content.Context
import android.os.SystemClock
import com.alarm.critical.service.AlarmSoundManager

/**
 * INV-ANDROID-308 (2026-06-02) — sonnerie locale déclenchée par SMS de commande
 * envoyé par le backend.
 *
 * Architecture business :
 * - Le backend détecte qu'un opérateur d'astreinte est offline (heartbeat KO).
 * - Il envoie un SMS spécial via la gateway SIM7600, format `[ALARME-MURGAT-WAKE] ...`.
 * - L'app capte ce SMS via `SmsWakeReceiver` (permission RECEIVE_SMS) et appelle
 *   `SmsWakeAlarmController.trigger()`. La sonnerie démarre, l'opérateur sait
 *   qu'il est inopérant côté backend et qu'il doit se déplacer.
 *
 * Pourquoi ce design (vs détection locale "noNetwork" — cf INV-ANDROID-302/305) :
 * sur Android moderne, aucune API tierce ne donne un état réseau fiable
 * (ServiceState redacté, callbacks pas tirés sur certaines ROMs Crosscall).
 * La seule source de vérité opérationnelle est le test concret côté serveur
 * (heartbeat HTTP + delivery report SMS de la gateway Orange/Sosh).
 *
 * Snooze : 5 min × 3 max par épisode (réutilise sémantique INV-ANDROID-307).
 * Reset du quota au prochain heartbeat 2xx (cf `AlarmPollingService`).
 */
object SmsWakeAlarmController {

    const val SNOOZE_DURATION_MS = 5 * 60 * 1000L
    const val SNOOZE_MAX_COUNT = 3

    @Volatile var active: Boolean = false
        private set
    @Volatile var snoozeUntilElapsedMs: Long = 0L
        private set
    @Volatile var snoozeCount: Int = 0
        private set

    private var soundManager: AlarmSoundManager? = null

    /**
     * Déclenche la sonnerie locale. Appelé par `SmsWakeReceiver` à réception
     * d'un SMS dont le payload matche `SmsWakeReceiver.WAKE_PREFIX`.
     *
     * Idempotent : appelé une 2e fois pendant que la sonnerie est déjà active,
     * ne fait rien. Pendant un snooze actif, **ne ré-arme pas** la sonnerie
     * (le snooze est respecté).
     */
    @Synchronized
    fun trigger(context: Context) {
        val now = SystemClock.elapsedRealtime()
        if (isSnoozed(now)) {
            AppLogger.log(
                "SmsWake",
                "trigger ignore : en cours de snooze (${snoozeRemainingMs(now) / 1000L}s restants)"
            )
            return
        }
        if (active) {
            AppLogger.log("SmsWake", "trigger redondant : sonnerie deja active")
            return
        }
        active = true
        val appCtx = context.applicationContext
        if (soundManager == null) {
            soundManager = AlarmSoundManager(appCtx)
        }
        soundManager?.startAlarmSound()
        AppLogger.log("SmsWake", "INV-308 sonnerie locale armee suite a SMS wake")
    }

    /**
     * INV-ANDROID-307 / 308 : snooze 5 min de la sonnerie. Retourne `true` si
     * snooze armé, `false` si quota épuisé.
     */
    @Synchronized
    fun snooze(): Boolean {
        if (snoozeCount >= SNOOZE_MAX_COUNT) {
            AppLogger.log(
                "SmsWake",
                "Snooze refuse : quota epuise ($snoozeCount/$SNOOZE_MAX_COUNT)"
            )
            return false
        }
        snoozeCount += 1
        snoozeUntilElapsedMs = SystemClock.elapsedRealtime() + SNOOZE_DURATION_MS
        active = false
        soundManager?.stopAlarmSound()
        AppLogger.log(
            "SmsWake",
            "Sonnerie INV-308 mise en sourdine 5 min (snooze $snoozeCount/$SNOOZE_MAX_COUNT)"
        )
        return true
    }

    /**
     * Reset complet — appelé par `AlarmPollingService` quand un heartbeat 2xx
     * revient (= fin d'épisode). Coupe le son, reset le quota snooze.
     */
    @Synchronized
    fun reset() {
        val wasActive = active
        val hadSnooze = snoozeUntilElapsedMs != 0L || snoozeCount != 0
        active = false
        snoozeUntilElapsedMs = 0L
        snoozeCount = 0
        soundManager?.stopAlarmSound()
        if (wasActive || hadSnooze) {
            AppLogger.log(
                "SmsWake",
                "Reset complet (wasActive=$wasActive hadSnooze=$hadSnooze)"
            )
        }
    }

    fun isSnoozed(now: Long = SystemClock.elapsedRealtime()): Boolean {
        return snoozeUntilElapsedMs > 0L && now < snoozeUntilElapsedMs
    }

    fun snoozeRemainingMs(now: Long = SystemClock.elapsedRealtime()): Long {
        return (snoozeUntilElapsedMs - now).coerceAtLeast(0L)
    }
}
