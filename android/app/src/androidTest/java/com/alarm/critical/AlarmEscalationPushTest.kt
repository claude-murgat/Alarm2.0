package com.alarm.critical

import android.content.Context
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.alarm.critical.service.AlarmWakeUpHandler
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

/**
 * INV-ANDROID-601 — un push `escalation_update` met a jour les prefs ET
 * reconcilie le heartbeat.
 *
 * Teste `AlarmWakeUpHandler` en isolation, sans Firebase (c'est la forme de test
 * que le catalogue appelle pour INV-ANDROID-600/601).
 *
 * Non couvert ici : le demarrage/arret effectif du foreground service, qui
 * demande un device et la permission de notification. Ces assertions portent sur
 * la partie deterministe : les transitions de prefs. La garde `started_by_fcm`
 * (une retrogradation ne coupe pas un service reveille par une ALARME) n'est pas
 * testable via les prefs — le handler n'y touche pas — et reste a verifier
 * manuellement.
 */
@RunWith(AndroidJUnit4::class)
class AlarmEscalationPushTest {

    private lateinit var context: Context

    private fun prefs() =
        context.getSharedPreferences("alarm_prefs", Context.MODE_PRIVATE)

    @Before
    fun setUp() {
        context = InstrumentationRegistry.getInstrumentation().targetContext
        prefs().edit().clear().apply()
    }

    @Test
    fun promotion_en_position_1_passe_is_oncall_a_vrai() {
        prefs().edit()
            .putString("token", "jeton-de-test")
            .putBoolean("is_oncall", false)
            .putInt("escalation_position", 2)
            .apply()

        AlarmWakeUpHandler.onEscalationPushReceived(context, position = 1, isOncall = true)

        assertTrue(
            "un push de promotion doit passer is_oncall a vrai",
            prefs().getBoolean("is_oncall", false)
        )
        assertEquals(
            "la position doit etre celle du push",
            1, prefs().getInt("escalation_position", -1)
        )
    }

    @Test
    fun retrogradation_passe_is_oncall_a_faux() {
        prefs().edit()
            .putString("token", "jeton-de-test")
            .putBoolean("is_oncall", true)
            .putInt("escalation_position", 1)
            .apply()

        AlarmWakeUpHandler.onEscalationPushReceived(context, position = 3, isOncall = false)

        assertFalse(
            "un push de retrogradation doit passer is_oncall a faux",
            prefs().getBoolean("is_oncall", true)
        )
        assertEquals(3, prefs().getInt("escalation_position", -1))
    }

    @Test
    fun promotion_sans_token_stocke_ne_crashe_pas_et_ecrit_quand_meme_les_prefs() {
        // Pas de token : le heartbeat ne peut pas demarrer, mais la position doit
        // tout de meme etre enregistree pour l'affichage et pour le prochain login.
        prefs().edit().putBoolean("is_oncall", false).apply()

        AlarmWakeUpHandler.onEscalationPushReceived(context, position = 1, isOncall = true)

        assertTrue(prefs().getBoolean("is_oncall", false))
        assertEquals(1, prefs().getInt("escalation_position", -1))
    }
}
