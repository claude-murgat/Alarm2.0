package com.alarm.critical

import android.app.Notification
import android.app.NotificationManager
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.SystemClock
import android.widget.TextView
import androidx.lifecycle.Lifecycle
import androidx.test.core.app.ActivityScenario
import androidx.test.espresso.Espresso.onView
import androidx.test.espresso.IdlingRegistry
import androidx.test.espresso.action.ViewActions.click
import androidx.test.espresso.assertion.ViewAssertions.doesNotExist
import androidx.test.espresso.assertion.ViewAssertions.matches
import androidx.test.espresso.matcher.ViewMatchers.Visibility
import androidx.test.espresso.matcher.ViewMatchers.isDisplayed
import androidx.test.espresso.matcher.ViewMatchers.withContentDescription
import androidx.test.espresso.matcher.ViewMatchers.withEffectiveVisibility
import androidx.test.espresso.matcher.ViewMatchers.withId
import androidx.test.espresso.matcher.ViewMatchers.withSubstring
import androidx.test.espresso.matcher.ViewMatchers.withText
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.alarm.critical.api.ApiProvider
import com.alarm.critical.model.AlarmResponse
import com.alarm.critical.service.AlarmPollingService
import com.alarm.critical.service.AlarmSoundManager
import com.alarm.critical.service.AlarmWakeUpHandler
import org.hamcrest.Matchers.allOf
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import retrofit2.Response

/**
 * INV-ANDROID-009 : multi-site. L'app traite toutes les alarmes en cours (une par
 * site, INV-155) : site affiche en gros, une ligne + un bouton d'acquittement par
 * alarme, sonnerie tant qu'il en reste une a acquitter.
 */
@RunWith(AndroidJUnit4::class)
class AlarmMultiSiteTest {

    private lateinit var context: Context
    private lateinit var fakeApi: FakeApiService
    private val scenarios = mutableListOf<ActivityScenario<*>>()

    private fun prefs() = context.getSharedPreferences("alarm_prefs", Context.MODE_PRIVATE)

    // Attente sur condition (pas de delai fixe) : sonnerie et tick 1 s de l'ecran d'alarme
    private fun waitFor(timeoutMs: Long = 8000, cond: () -> Boolean): Boolean {
        val end = SystemClock.uptimeMillis() + timeoutMs
        while (SystemClock.uptimeMillis() < end) {
            if (cond()) return true
            SystemClock.sleep(100)
        }
        return cond()
    }

    private fun alarm(id: Int, site: String?, title: String) = AlarmResponse(
        id = id, title = title, message = "multi-site", severity = "critical",
        status = "active", assigned_user_id = 1, acknowledged_at = null,
        acknowledged_by_name = null, suspended_until = null, escalation_count = 0,
        created_at = "2026-10-23T08:00:00", site = site
    )

    private fun acked(a: AlarmResponse) = a.copy(
        status = "acknowledged", acknowledged_at = "2026-10-23T08:01:00",
        acknowledged_by_name = "user1", ack_remaining_seconds = 1800
    )

    private val izeron = alarm(101, "izeron", "Declenchement contact sec - Izeron")
    private val beaufort = alarm(102, "beaufort", "Declenchement contact sec - Beaufort")

    @Before
    fun setUp() {
        context = InstrumentationRegistry.getInstrumentation().targetContext
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            InstrumentationRegistry.getInstrumentation().uiAutomation
                .grantRuntimePermission(context.packageName, "android.permission.POST_NOTIFICATIONS")
        }
        fakeApi = FakeApiService()
        fakeApi.acknowledgeResponses = mapOf(
            izeron.id to Response.success(acked(izeron)),
            beaufort.id to Response.success(acked(beaufort))
        )
        ApiProvider.override(fakeApi)
        prefs().edit().clear()
            .putString("token", "fake-token")
            .putString("user_name", "user1")
            .putInt("user_id", 1)
            .putBoolean("battery_optimization_asked", true)
            .putBoolean("fsi_permission_asked", true)
            .commit()
        AlarmPollingService.alarms = emptyList()
    }

    @After
    fun tearDown() {
        IdlingRegistry.getInstance().resources.forEach { IdlingRegistry.getInstance().unregister(it) }
        scenarios.reversed().forEach { it.close() }
        context.stopService(Intent(context, AlarmPollingService::class.java))
        waitFor { !AlarmPollingService.isRunning }
        AlarmSoundManager(context).stopAlarmSound()
        AlarmPollingService.alarms = emptyList()
        ApiProvider.reset()
        prefs().edit().clear().commit()
    }

    /** Alarmes vues par le dernier poll, puis ecran d'alarme ouvert. */
    private fun openAlarmScreen(vararg alarms: AlarmResponse): ActivityScenario<AlarmActivity> {
        AlarmPollingService.alarms = alarms.toList()
        val scenario = ActivityScenario.launch<AlarmActivity>(
            Intent(context, AlarmActivity::class.java)
                .putExtra("alarm_id", alarms[0].id)
                .putExtra("token", "fake-token")
        )
        scenarios += scenario
        return scenario
    }

    // Le son de test boucle sur un fichier court : isPlaying() repasse a false entre
    // deux boucles. On echantillonne donc sur plus d'une boucle (cf AlarmTestAccountTest).
    private fun ringsWithin3s() = waitFor(3000) { AlarmSoundManager(context).isPlaying() }

    @Test
    fun une_alarme_affiche_son_site_en_gros() {
        openAlarmScreen(beaufort)

        onView(withId(R.id.alarmSite)).check(matches(withText("Beaufort")))
        onView(withId(R.id.alarmList)).check(matches(withEffectiveVisibility(Visibility.GONE)))
        onView(withId(R.id.ackButton)).check(matches(isDisplayed()))
    }

    @Test
    fun alarme_sans_site_affiche_astreinte() {
        openAlarmScreen(alarm(103, null, "Perte de heartbeat"))

        onView(withId(R.id.alarmSite)).check(matches(withText("Astreinte")))
    }

    @Test
    fun deux_alarmes_listees_chacune_avec_son_bouton() {
        openAlarmScreen(izeron, beaufort)

        onView(withId(R.id.alarmList)).check(matches(isDisplayed()))
        onView(withText("Izeron")).check(matches(isDisplayed()))
        onView(withText("Beaufort")).check(matches(isDisplayed()))
        onView(allOf(withText(izeron.title), isDisplayed())).check(matches(isDisplayed()))
        onView(allOf(withText(beaufort.title), isDisplayed())).check(matches(isDisplayed()))
        onView(withContentDescription("Acquitter Izeron")).check(matches(isDisplayed()))
        onView(withContentDescription("Acquitter Beaufort")).check(matches(isDisplayed()))
        // Pas de bouton unique ambigu : chaque alarme a le sien
        onView(withId(R.id.ackButton)).check(matches(withEffectiveVisibility(Visibility.GONE)))
    }

    @Test
    fun acquitter_une_alarme_laisse_l_autre_sonner() {
        openAlarmScreen(izeron, beaufort)
        assertTrue("temoin : l'alarme sonne sur cet appareil", ringsWithin3s())

        onView(withContentDescription("Acquitter Izeron")).perform(click())

        assertEquals("seule Izeron est acquittee", listOf(izeron.id), fakeApi.acknowledgedIds)
        onView(withContentDescription("Acquitter Izeron")).check(doesNotExist())
        onView(withContentDescription("Acquitter Beaufort")).check(matches(isDisplayed()))
        onView(withId(R.id.ackConfirmation)).check(matches(withEffectiveVisibility(Visibility.GONE)))
        assertTrue("Beaufort reste a acquitter : la sonnerie continue", ringsWithin3s())
    }

    @Test
    fun acquitter_les_deux_arrete_la_sonnerie() {
        openAlarmScreen(izeron, beaufort)

        onView(withContentDescription("Acquitter Izeron")).perform(click())
        onView(withContentDescription("Acquitter Beaufort")).perform(click())

        assertEquals(listOf(izeron.id, beaufort.id), fakeApi.acknowledgedIds)
        onView(withId(R.id.ackConfirmation)).check(matches(isDisplayed()))
        assertFalse("toutes acquittees : plus de sonnerie", ringsWithin3s())
    }

    /** INV-ANDROID-210 par alarme : la resolution d'Izeron ne fait pas taire Beaufort. */
    @Test
    fun resolution_d_une_alarme_laisse_l_autre_affichee_et_sonnante() {
        val scenario = openAlarmScreen(izeron, beaufort)

        AlarmWakeUpHandler.onAlarmResolvedPushReceived(context, izeron.id.toString())

        assertEquals(listOf(beaufort.id), AlarmPollingService.alarms.map { it.id })
        assertTrue("l'ecran affiche l'alarme restante", waitFor {
            var site = ""
            scenario.onActivity { site = it.findViewById<TextView>(R.id.alarmSite).text.toString() }
            site == "Beaufort"
        })
        assertNotEquals("l'ecran reste ouvert", Lifecycle.State.DESTROYED, scenario.state)
        assertTrue("Beaufort reste a acquitter : la sonnerie continue", ringsWithin3s())
    }

    @Test
    fun dashboard_et_notifications_indiquent_les_sites_et_le_nombre() {
        fakeApi.myAlarmsResponses = mutableListOf(Response.success(listOf(izeron, beaufort)))
        val polls = PollingIdlingResource("multisite-poll-${System.nanoTime()}", 2)
        fakeApi.pollingIdlingResource = polls
        IdlingRegistry.getInstance().register(polls)
        scenarios += ActivityScenario.launch<DashboardActivity>(
            Intent(context, DashboardActivity::class.java).putExtra("token", "fake-token")
        )

        onView(withId(R.id.currentAlarmLine)).check(matches(allOf(
            withSubstring("2 alarmes"), withSubstring("Izeron"), withSubstring("Beaufort")
        )))

        val manager = context.getSystemService(NotificationManager::class.java)
        fun text(id: Int) = manager.activeNotifications.firstOrNull { it.id == id }?.notification?.extras
            ?.let { "${it.getCharSequence(Notification.EXTRA_TITLE)} ${it.getCharSequence(Notification.EXTRA_TEXT)}" }
            .orEmpty()
        for (id in listOf(AlarmPollingService.ALARM_NOTIFICATION_ID, AlarmPollingService.NOTIFICATION_ID)) {
            val shown = text(id)
            assertTrue("notification $id : '$shown'",
                shown.contains("2 alarmes") && shown.contains("Izeron") && shown.contains("Beaufort"))
        }
    }
}
