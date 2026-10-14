package com.alarm.critical

import android.app.NotificationManager
import android.content.Context
import android.content.Intent
import android.os.SystemClock
import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.alarm.critical.api.ApiProvider
import com.alarm.critical.model.EscalationEntry
import com.alarm.critical.service.AlarmPollingService
import com.alarm.critical.service.BootReceiver
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import retrofit2.Response

/**
 * Le heartbeat doit survivre a la vie reelle d'un telephone d'astreinte
 * (bug terrain 2026-10-14 : "obligé de me déconnecter/reconnecter pour
 * apparaître en ligne", "hors ligne quand le mobile est en veille sur wifi").
 *
 * INV-ANDROID-309 (veille), INV-ANDROID-606 (astreinte resynchronisee),
 * INV-ANDROID-704 (boot + relance systeme), INV-ANDROID-111 (icone notif).
 */
@RunWith(AndroidJUnit4::class)
class AlarmResilienceTest {

    private lateinit var context: Context
    private lateinit var fakeApi: FakeApiService

    private fun prefs() = context.getSharedPreferences("alarm_prefs", Context.MODE_PRIVATE)

    /** Attente bornee sur une condition observable (pas de sleep aveugle). */
    private fun waitFor(timeoutMs: Long = 5000, cond: () -> Boolean): Boolean {
        val end = SystemClock.uptimeMillis() + timeoutMs
        while (SystemClock.uptimeMillis() < end) {
            if (cond()) return true
            SystemClock.sleep(100)
        }
        return cond()
    }

    @Before
    fun setUp() {
        context = InstrumentationRegistry.getInstrumentation().targetContext
        fakeApi = FakeApiService()
        ApiProvider.override(fakeApi)
        prefs().edit().clear()
            .putString("token", "fake-token")
            .putInt("user_id", 1)
            .putBoolean("battery_optimization_asked", true)
            .commit()
    }

    @After
    fun tearDown() {
        context.stopService(Intent(context, AlarmPollingService::class.java))
        waitFor { !AlarmPollingService.isRunning }
        ApiProvider.reset()
        prefs().edit().clear().commit()
    }

    private fun startServiceWithToken() {
        context.startForegroundService(
            Intent(context, AlarmPollingService::class.java)
                .putExtra(AlarmPollingService.EXTRA_TOKEN, "fake-token")
        )
        assertTrue("le service doit demarrer", waitFor { AlarmPollingService.isRunning })
    }

    // ── INV-ANDROID-606 : statut d'astreinte relu a chaque ouverture ──────

    @Test
    fun ouverture_app_reprend_l_astreinte_depuis_la_chaine() {
        prefs().edit().putBoolean("is_oncall", false).putInt("escalation_position", 2).commit()
        fakeApi.escalationChainResponse = Response.success(
            listOf(EscalationEntry(position = 1, user_id = 1), EscalationEntry(position = 2, user_id = 4))
        )

        val scenario = ActivityScenario.launch(DashboardActivity::class.java)

        assertTrue("promu en position 1 -> is_oncall", waitFor { prefs().getBoolean("is_oncall", false) })
        assertEquals(1, prefs().getInt("escalation_position", -1))
        assertTrue("astreinte -> heartbeat demarre", waitFor { AlarmPollingService.isRunning })
        scenario.close()
    }

    @Test
    fun ouverture_app_retrograde_si_plus_en_position_1() {
        prefs().edit().putBoolean("is_oncall", true).putInt("escalation_position", 1).commit()
        fakeApi.escalationChainResponse = Response.success(
            listOf(EscalationEntry(position = 1, user_id = 4), EscalationEntry(position = 2, user_id = 1))
        )

        val scenario = ActivityScenario.launch(DashboardActivity::class.java)

        assertTrue("plus en position 1 -> plus d'astreinte",
            waitFor { !prefs().getBoolean("is_oncall", true) })
        assertEquals(2, prefs().getInt("escalation_position", -1))
        scenario.close()
    }

    // ── INV-ANDROID-704 : relance systeme et demarrage du telephone ───────

    @Test
    fun relance_par_android_sans_intent_reprend_le_token_sauvegarde() {
        // START_STICKY : Android relance le service avec un intent sans extras.
        context.startForegroundService(Intent(context, AlarmPollingService::class.java))

        assertTrue("le service ne doit pas s'arreter faute de token dans l'intent",
            waitFor { AlarmPollingService.isRunning })
    }

    @Test
    fun demarrage_du_telephone_relance_le_heartbeat_si_astreinte() {
        prefs().edit().putBoolean("is_oncall", true).putInt("escalation_position", 1).commit()

        BootReceiver().onReceive(context, Intent(Intent.ACTION_BOOT_COMPLETED))

        assertTrue(waitFor { AlarmPollingService.isRunning })
    }

    @Test
    fun demarrage_du_telephone_ne_relance_rien_hors_astreinte() {
        prefs().edit().putBoolean("is_oncall", false).putInt("escalation_position", 2).commit()

        BootReceiver().onReceive(context, Intent(Intent.ACTION_BOOT_COMPLETED))

        assertFalse(waitFor(1500) { AlarmPollingService.isRunning })
    }

    // ── INV-ANDROID-309 : CPU et Wi-Fi maintenus eveilles en veille ───────

    @Test
    fun service_garde_le_cpu_et_le_wifi_eveilles() {
        startServiceWithToken()

        assertTrue("WakeLock partiel tenu", AlarmPollingService.wakeLock?.isHeld == true)
        assertTrue("WifiLock tenu", AlarmPollingService.wifiLock?.isHeld == true)
    }

    // ── INV-ANDROID-111 : icone de l'app dans les notifications ───────────

    @Test
    fun notification_du_service_utilise_l_icone_de_l_app() {
        startServiceWithToken()

        val nm = context.getSystemService(NotificationManager::class.java)
        val notif = nm.activeNotifications.firstOrNull { it.id == AlarmPollingService.NOTIFICATION_ID }
        assertTrue("notification du service affichee", notif != null)
        assertEquals(R.drawable.ic_stat_alarm, notif!!.notification.smallIcon.resId)
    }
}
