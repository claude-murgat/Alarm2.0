package com.alarm.critical

import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.SystemClock
import android.view.View
import androidx.test.core.app.ActivityScenario
import androidx.test.espresso.Espresso.onView
import androidx.test.espresso.action.ViewActions.clearText
import androidx.test.espresso.action.ViewActions.click
import androidx.test.espresso.action.ViewActions.closeSoftKeyboard
import androidx.test.espresso.action.ViewActions.typeText
import androidx.test.espresso.matcher.ViewMatchers.withId
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.alarm.critical.api.ApiProvider
import com.alarm.critical.model.AlarmResponse
import com.alarm.critical.model.TokenResponse
import com.alarm.critical.model.UserResponse
import com.alarm.critical.service.AlarmPollingService
import com.alarm.critical.service.AlarmSoundManager
import org.junit.After
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import retrofit2.Response

/**
 * INV-ANDROID-008 : un compte test (is_test, INV-151) voit l'alarme mais son
 * telephone ne sonne pas. Le compte normal sert de temoin : sans lui, un
 * appareil incapable de jouer le son ferait passer le test a tort.
 */
@RunWith(AndroidJUnit4::class)
class AlarmTestAccountTest {

    private lateinit var context: Context
    private lateinit var fakeApi: FakeApiService
    private val scenarios = mutableListOf<ActivityScenario<*>>()

    private fun prefs() = context.getSharedPreferences("alarm_prefs", Context.MODE_PRIVATE)

    private fun waitFor(timeoutMs: Long = 15000, cond: () -> Boolean): Boolean {
        val end = SystemClock.uptimeMillis() + timeoutMs
        while (SystemClock.uptimeMillis() < end) {
            if (cond()) return true
            SystemClock.sleep(100)
        }
        return cond()
    }

    private val alarm = AlarmResponse(
        id = 51, title = "INV-008", message = "compte test", severity = "critical",
        status = "active", assigned_user_id = 1, acknowledged_at = null,
        acknowledged_by_name = null, suspended_until = null, escalation_count = 0,
        created_at = "2026-10-22T08:00:00"
    )

    @Before
    fun setUp() {
        context = InstrumentationRegistry.getInstrumentation().targetContext
        // Sans ca, la demande de permission de MainActivity masque l'ecran de login
        val ui = InstrumentationRegistry.getInstrumentation().uiAutomation
        ui.grantRuntimePermission(context.packageName, "android.permission.READ_PHONE_STATE")
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            ui.grantRuntimePermission(context.packageName, "android.permission.POST_NOTIFICATIONS")
        }
        fakeApi = FakeApiService()
        ApiProvider.override(fakeApi)
        prefs().edit().clear()
            .putString("token", "fake-token")
            .putInt("user_id", 1)
            // Pas de dialogue systeme pendant le test
            .putBoolean("battery_optimization_asked", true)
            .putBoolean("fsi_permission_asked", true)
            .commit()
        AlarmPollingService.currentAlarm = null
    }

    @After
    fun tearDown() {
        scenarios.reversed().forEach { it.close() }
        context.stopService(Intent(context, AlarmPollingService::class.java))
        waitFor { !AlarmPollingService.isRunning }
        AlarmSoundManager(context).stopAlarmSound()
        AlarmPollingService.currentAlarm = null
        ApiProvider.reset()
        prefs().edit().clear().commit()
    }

    // Le son de test boucle sur un fichier court : isPlaying() repasse a false entre
    // deux boucles. On echantillonne donc sur plus d'une boucle.
    private fun ringsWithin3s() = waitFor(3000) { AlarmSoundManager(context).isPlaying() }

    /** Alarme active vue par le service, le dashboard et l'ecran d'alarme. */
    private fun showActiveAlarmEverywhere() {
        fakeApi.myAlarmsResponses = mutableListOf(Response.success(listOf(alarm)))
        val dashboard = ActivityScenario.launch<DashboardActivity>(
            Intent(context, DashboardActivity::class.java).putExtra("token", "fake-token")
        ).also { scenarios += it }
        assertTrue("le dashboard affiche l'alarme", waitFor {
            var shown = false
            dashboard.onActivity {
                shown = it.findViewById<View>(R.id.dashboardAckButton).visibility == View.VISIBLE
            }
            shown
        })
        // Deux polls de plus : la demande de sonnerie du service est passee.
        val polls = fakeApi.myAlarmsCallCount
        assertTrue("le service continue de poller", waitFor { fakeApi.myAlarmsCallCount >= polls + 2 })
        scenarios += ActivityScenario.launch<AlarmActivity>(
            Intent(context, AlarmActivity::class.java)
                .putExtra("alarm_id", alarm.id)
                .putExtra("token", "fake-token")
        )
        assertTrue("ecran d'alarme affiche", waitFor { AlarmActivity.isVisible })
    }

    @Test
    fun compte_test_alarme_affichee_sans_sonnerie() {
        prefs().edit().putBoolean("is_test", true).commit()

        showActiveAlarmEverywhere()

        assertFalse("un compte test ne doit pas faire sonner l'alarme", ringsWithin3s())
    }

    @Test
    fun compte_normal_alarme_sonne() {
        showActiveAlarmEverywhere()

        assertTrue("temoin : un compte normal sonne sur cet appareil", ringsWithin3s())
    }

    @Test
    fun login_memorise_le_role_test() {
        prefs().edit().remove("token").commit()  // sinon MainActivity file au dashboard
        fakeApi.loginResponse = Response.success(TokenResponse(
            access_token = "fake-token", token_type = "bearer",
            user = UserResponse(1, "testeur@alarm.local", "testeur", false, true, is_test = true)
        ))
        scenarios += ActivityScenario.launch(MainActivity::class.java)

        onView(withId(R.id.nameInput)).perform(clearText(), typeText("testeur"), closeSoftKeyboard())
        onView(withId(R.id.passwordInput)).perform(clearText(), typeText("pass1234"), closeSoftKeyboard())
        onView(withId(R.id.loginButton)).perform(click())

        assertTrue("le role test doit etre memorise au login",
            waitFor { prefs().getBoolean("is_test", false) })
    }
}
