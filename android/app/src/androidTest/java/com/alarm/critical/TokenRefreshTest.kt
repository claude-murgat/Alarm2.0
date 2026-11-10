package com.alarm.critical

import android.content.Context
import android.content.ContextWrapper
import android.content.Intent
import android.os.Build
import android.os.SystemClock
import android.view.accessibility.AccessibilityEvent
import androidx.test.core.app.ActivityScenario
import androidx.test.espresso.Espresso.onView
import androidx.test.espresso.action.ViewActions.click
import androidx.test.espresso.action.ViewActions.scrollTo
import androidx.test.espresso.assertion.ViewAssertions.doesNotExist
import androidx.test.espresso.assertion.ViewAssertions.matches
import androidx.test.espresso.matcher.ViewMatchers.Visibility
import androidx.test.espresso.matcher.ViewMatchers.isDisplayed
import androidx.test.espresso.matcher.ViewMatchers.withEffectiveVisibility
import androidx.test.espresso.matcher.ViewMatchers.withId
import androidx.test.espresso.matcher.ViewMatchers.withText
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.alarm.critical.api.ApiProvider
import com.alarm.critical.api.Auth
import com.alarm.critical.model.AlarmResponse
import com.alarm.critical.model.EscalationEntry
import com.alarm.critical.model.UserResponse
import com.alarm.critical.service.AlarmFirebaseService
import com.alarm.critical.service.AlarmPollingService
import com.alarm.critical.service.AlarmSoundManager
import com.alarm.critical.util.AppLogger
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import retrofit2.Response
import java.util.concurrent.TimeoutException

/**
 * INV-ANDROID-508 : chaque appel d'un ecran lit le token courant des prefs ; sur 401, un seul
 * renouvellement via POST /auth/refresh (refresh_token des prefs), puis la requete est rejouee une fois.
 *
 * Demande du proprietaire (2026-11-09, app 1.9.4) : « Échec (HTTP 401) Invalid token » en changeant
 * d'ecran une fois l'access token (24 h) expire. user1 est ici hors astreinte : pas de service de
 * polling, donc personne d'autre ne renouvelle le token.
 */
@RunWith(AndroidJUnit4::class)
class TokenRefreshTest {

    private lateinit var context: Context
    private lateinit var fakeApi: FakeApiService
    private val scenarios = mutableListOf<ActivityScenario<*>>()

    private fun prefs() = context.getSharedPreferences("alarm_prefs", Context.MODE_PRIVATE)

    private val active = AlarmResponse(
        id = 42, title = "Contact sec", message = "msg", severity = "critical", status = "active",
        assigned_user_id = 1, acknowledged_at = null, acknowledged_by_name = null, suspended_until = null,
        escalation_count = 0, created_at = "2026-11-09T08:00:00", site = "izeron"
    )

    private fun waitFor(timeoutMs: Long = 10_000, cond: () -> Boolean): Boolean {
        val end = SystemClock.uptimeMillis() + timeoutMs
        while (SystemClock.uptimeMillis() < end) {
            if (cond()) return true
            SystemClock.sleep(100)
        }
        return cond()
    }

    /** Attente bornee d'une assertion (INV-ANDROID-801) : le dashboard affiche au tick 1 Hz suivant. */
    private fun eventually(timeoutMs: Long = 10_000, assertion: () -> Unit) {
        val end = SystemClock.uptimeMillis() + timeoutMs
        while (true) {
            try {
                return assertion()
            } catch (e: Throwable) {
                if (SystemClock.uptimeMillis() > end) throw e
            }
            SystemClock.sleep(200)
        }
    }

    /** Vrai si un toast contenant [text] s'affiche pendant [action] (evenement d'accessibilite du toast). */
    private fun toastShown(text: String, action: () -> Unit): Boolean = try {
        InstrumentationRegistry.getInstrumentation().uiAutomation.executeAndWaitForEvent(
            { action() },
            { e -> e.eventType == AccessibilityEvent.TYPE_NOTIFICATION_STATE_CHANGED && e.text.joinToString().contains(text) },
            10_000
        )
        true
    } catch (_: TimeoutException) {
        false
    }

    private fun launchDashboard() {
        scenarios += ActivityScenario.launch<DashboardActivity>(
            Intent(context, DashboardActivity::class.java).putExtra("token", "fake-token")
        )
    }

    @Before
    fun setUp() {
        context = InstrumentationRegistry.getInstrumentation().targetContext
        val ui = InstrumentationRegistry.getInstrumentation().uiAutomation
        ui.grantRuntimePermission(context.packageName, "android.permission.READ_PHONE_STATE")
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            ui.grantRuntimePermission(context.packageName, "android.permission.POST_NOTIFICATIONS")
        }
        fakeApi = FakeApiService().apply {
            escalationChainResponse = Response.success(listOf(EscalationEntry(position = 1, user_id = 2)))
            alarmHistoryResponse = Response.success(listOf(active.copy(id = 41, status = "resolved")))
        }
        ApiProvider.override(fakeApi)
        prefs().edit().clear()
            .putString("token", "fake-token")
            .putString("refresh_token", "refresh-uuid-du-login")
            .putString("user_name", "user1")
            .putInt("user_id", 1)
            .putBoolean("is_oncall", false)
            .putBoolean("battery_optimization_asked", true)
            .putBoolean("fsi_permission_asked", true)
            .commit()
        AlarmPollingService.alarms = emptyList()
    }

    @After
    fun tearDown() {
        scenarios.reversed().forEach { it.close() }
        context.stopService(Intent(context, AlarmPollingService::class.java))
        waitFor { !AlarmPollingService.isRunning }
        AlarmSoundManager(context).stopAlarmSound()
        AlarmPollingService.alarms = emptyList()
        ApiProvider.reset()
        prefs().edit().clear().commit()
    }

    /** Le bug signale : token expire, on ouvre un ecran du menu. */
    @Test
    fun ecran_du_menu_avec_token_expire_renouvelle_puis_affiche() {
        fakeApi.expiredTokens += "fake-token"

        scenarios += ActivityScenario.launch(AlarmsActivity::class.java)

        onView(withText("Izeron · Contact sec")).perform(scrollTo()).check(matches(isDisplayed()))
        assertEquals("un seul refresh", 1, fakeApi.refreshCallCount)
        assertEquals("refresh_token des prefs envoye", "refresh-uuid-du-login",
            fakeApi.lastRefreshRequestPayload?.refresh_token)
        assertEquals("nouveau token en prefs", "fake-refreshed-token", prefs().getString("token", null))
    }

    /** Refresh refuse : l'erreur d'origine comme avant, une seule tentative, pas de deconnexion. */
    @Test
    fun refresh_refuse_garde_l_erreur_sans_boucle_ni_deconnexion() {
        fakeApi.expiredTokens += "fake-token"
        fakeApi.refreshTokenResponse = Response.error(
            401, okhttp3.ResponseBody.create(null, """{"detail":"Invalid refresh token"}""")
        )

        assertTrue("toast d'erreur d'origine", toastShown("Échec (HTTP 401) : Invalid token") {
            scenarios += ActivityScenario.launch(AlarmsActivity::class.java)
        })

        onView(withText("Izeron · Contact sec")).check(doesNotExist())
        assertEquals("une seule tentative de refresh", 1, fakeApi.refreshCallCount)
        assertEquals("pas de deconnexion", "refresh-uuid-du-login", prefs().getString("refresh_token", null))
        assertEquals("token inchange", "fake-token", prefs().getString("token", null))
    }

    /** Dashboard ouvert avant l'expiration : l'acquittement renouvelle et rejoue avec le nouveau token. */
    @Test
    fun acquittement_du_dashboard_apres_expiration_renouvelle_et_rejoue() {
        AlarmPollingService.alarms = listOf(active)
        launchDashboard()
        eventually { onView(withId(R.id.dashboardAckButton)).check(matches(isDisplayed())) }

        fakeApi.expiredTokens += "fake-token"  // 24 h plus tard, dashboard toujours ouvert
        onView(withId(R.id.dashboardAckButton)).perform(click())

        eventually { onView(withId(R.id.ackStatusText)).check(matches(withText("Acquittée"))) }
        assertEquals("ack rejoue avec le nouveau token", "Bearer fake-refreshed-token",
            fakeApi.acknowledgeCalledWith?.first)
        assertEquals("un seul refresh", 1, fakeApi.refreshCallCount)
    }

    /** Ecran d'alarme ouvert avec un token depuis renouvele en prefs : l'ack prend celui des prefs. */
    @Test
    fun acquittement_de_l_ecran_d_alarme_utilise_le_token_courant() {
        AlarmPollingService.alarms = listOf(active)
        scenarios += ActivityScenario.launch<AlarmActivity>(
            Intent(context, AlarmActivity::class.java).putExtra("alarm_id", 42).putExtra("token", "fake-token")
        )
        fakeApi.expiredTokens += "fake-token"
        prefs().edit().putString("token", "renewed-token").commit()  // renouvele par le service

        onView(withId(R.id.ackButton)).perform(click())

        eventually { onView(withId(R.id.ackConfirmation)).check(matches(isDisplayed())) }
        assertEquals("ack avec le token des prefs", "Bearer renewed-token", fakeApi.acknowledgeCalledWith?.first)
        assertEquals("token deja a jour : pas de refresh", 0, fakeApi.refreshCallCount)
    }

    /** Au demarrage du dashboard, /me et l'historique prennent 401 ensemble : un seul refresh. */
    @Test
    fun deux_401_simultanes_un_seul_refresh() {
        fakeApi.expiredTokens += "fake-token"
        fakeApi.refreshDelayMs = 500  // le 2e 401 arrive pendant le 1er refresh
        fakeApi.meResponse = Response.success(UserResponse(1, "", "user1", true, true))

        launchDashboard()

        eventually {
            onView(withId(R.id.alarmHistorySection)).check(matches(withEffectiveVisibility(Visibility.VISIBLE)))
        }
        eventually { assertTrue("role relu par /me rejoue", prefs().getBoolean("is_admin", false)) }
        assertEquals("un seul refresh pour deux 401", 1, fakeApi.refreshCallCount)
        assertEquals("fake-refreshed-token", prefs().getString("token", null))
    }

    /** 2026-11-10 : Firebase donne un nouveau token FCM alors que l'access token a expire. */
    @Test
    fun nouveau_token_fcm_avec_token_expire_renouvelle_puis_enregistre() {
        fakeApi.expiredTokens += "fake-token"
        prefs().edit().putString("device_token", "telephone-timo").commit()
        // Service cree hors de Firebase, avec le contexte que lui donnerait Android
        val service = AlarmFirebaseService()
        ContextWrapper::class.java.getDeclaredMethod("attachBaseContext", Context::class.java)
            .apply { isAccessible = true }.invoke(service, context)

        service.onNewToken("nouveau-token-fcm")

        assertTrue("nouveau token FCM enregistre", waitFor { fakeApi.fcmTokenRegistered?.token == "nouveau-token-fcm" })
        assertEquals("enregistre avec le token renouvele", "Bearer fake-refreshed-token", fakeApi.fcmTokenRegisteredWith)
        assertEquals("telephone-timo", fakeApi.fcmTokenRegistered?.device_id)
        assertEquals("un seul refresh", 1, fakeApi.refreshCallCount)
        service.onDestroy()
    }

    /**
     * 2026-11-10 : refresh impossible, l'historique du dashboard echoue toutes les 10 s et chaque echec
     * ajoutait une ligne (500 lignes en ~80 min). Une ligne par cause, puis a chaque changement de cause.
     */
    @Test
    fun refresh_impossible_une_ligne_de_log_par_cause() {
        fun screenCall() = runBlocking { Auth.call(context) { getAlarmHistory(it) } }  // le chemin de tous les ecrans
        fun lines(cause: String) = AppLogger.exportLogs("user1", "test").lines().count { cause in it }
        val rejected = "Refresh KO HTTP 401"
        val noRefreshToken = "Refresh impossible : refresh_token absent"

        fakeApi.expiredTokens += "fake-token"
        assertEquals("refresh reussi : le prochain echec sera ecrit", 200, screenCall().code())
        AppLogger.clear()
        fakeApi.expiredTokens += "fake-refreshed-token"  // expire a son tour, et le serveur refuse le refresh
        fakeApi.refreshTokenResponse = Response.error(
            401, okhttp3.ResponseBody.create(null, """{"detail":"Invalid refresh token"}""")
        )

        repeat(5) { assertEquals("erreur d'origine", 401, screenCall().code()) }
        assertEquals("5 echecs pour la meme cause : une ligne", 1, lines(rejected))

        prefs().edit().remove("refresh_token").commit()
        repeat(3) { screenCall() }
        assertEquals("nouvelle cause : une ligne", 1, lines(noRefreshToken))

        prefs().edit().putString("refresh_token", "refresh-uuid-du-login").commit()
        repeat(2) { screenCall() }
        assertEquals("retour a la premiere cause : ecrite de nouveau", 2, lines(rejected))
        assertEquals("chaque appel tente toujours son refresh", 1 + 5 + 2, fakeApi.refreshCallCount)
    }
}
