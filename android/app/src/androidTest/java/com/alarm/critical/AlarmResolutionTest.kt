package com.alarm.critical

import android.content.Context
import android.content.Intent
import android.os.SystemClock
import androidx.lifecycle.Lifecycle
import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.alarm.critical.api.ApiProvider
import com.alarm.critical.model.AlarmResponse
import com.alarm.critical.service.AlarmPollingService
import com.alarm.critical.service.AlarmSoundManager
import com.alarm.critical.service.AlarmWakeUpHandler
import org.junit.After
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import retrofit2.Response

/**
 * INV-ANDROID-210 : une alarme resolue coupe la sonnerie ET ferme l'ecran plein
 * ecran sur tous les telephones, meme verrouilles.
 *
 * Bug prod 2026-10-21 (alarme 886, resolue depuis le web) : le telephone de admin
 * a sonne jusqu'au deverrouillage, celui de timo a garde AlarmActivity affichee.
 */
@RunWith(AndroidJUnit4::class)
class AlarmResolutionTest {

    private lateinit var context: Context
    private lateinit var fakeApi: FakeApiService

    private fun prefs() = context.getSharedPreferences("alarm_prefs", Context.MODE_PRIVATE)

    private fun waitFor(timeoutMs: Long = 8000, cond: () -> Boolean): Boolean {
        val end = SystemClock.uptimeMillis() + timeoutMs
        while (SystemClock.uptimeMillis() < end) {
            if (cond()) return true
            SystemClock.sleep(100)
        }
        return cond()
    }

    private fun alarm(id: Int) = AlarmResponse(
        id = id, title = "INV-210", message = "resolution", severity = "critical",
        status = "active", assigned_user_id = 1, acknowledged_at = null,
        acknowledged_by_name = null, suspended_until = null, escalation_count = 0,
        created_at = "2026-10-21T08:00:00"
    )

    private fun launchAlarmScreen(id: Int): ActivityScenario<AlarmActivity> {
        val scenario = ActivityScenario.launch<AlarmActivity>(
            Intent(context, AlarmActivity::class.java)
                .putExtra("alarm_id", id)
                .putExtra("token", "fake-token")
        )
        assertTrue("ecran d'alarme affiche", waitFor { AlarmActivity.isVisible })
        return scenario
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
        AlarmPollingService.currentAlarm = null
    }

    @After
    fun tearDown() {
        context.stopService(Intent(context, AlarmPollingService::class.java))
        waitFor { !AlarmPollingService.isRunning }
        AlarmSoundManager(context).stopAlarmSound()
        AlarmPollingService.currentAlarm = null
        ApiProvider.reset()
        prefs().edit().clear().commit()
    }

    /** Telephone verrouille / Doze : seul le push de resolution arrive. */
    @Test
    fun push_de_resolution_coupe_la_sonnerie_et_ferme_l_ecran() {
        AlarmPollingService.currentAlarm = alarm(42)  // dernier poll avant Doze
        val scenario = launchAlarmScreen(42)

        AlarmWakeUpHandler.onAlarmResolvedPushReceived(context, "42")

        assertTrue("l'ecran d'alarme doit se fermer",
            waitFor { scenario.state == Lifecycle.State.DESTROYED })
        assertFalse("la sonnerie doit s'arreter", AlarmSoundManager(context).isPlaying())
        assertTrue("l'alarme resolue ne doit plus etre l'alarme courante",
            AlarmPollingService.currentAlarm == null)
    }

    /** Pas de push (perdu) : le poll qui ne voit plus l'alarme ferme l'ecran. */
    @Test
    fun poll_sans_alarme_ferme_l_ecran() {
        fakeApi.myAlarmsResponses = mutableListOf(Response.success(listOf(alarm(43))))
        val scenario = launchAlarmScreen(43)
        context.startForegroundService(
            Intent(context, AlarmPollingService::class.java)
                .putExtra(AlarmPollingService.EXTRA_TOKEN, "fake-token")
        )
        assertTrue("le service voit l'alarme", waitFor { AlarmPollingService.currentAlarm?.id == 43 })

        fakeApi.myAlarmsResponses = mutableListOf(Response.success(emptyList()))

        assertTrue("l'ecran d'alarme doit se fermer apres la resolution",
            waitFor { scenario.state == Lifecycle.State.DESTROYED })
        assertFalse("la sonnerie doit s'arreter", AlarmSoundManager(context).isPlaying())
    }
}
