package com.alarm.critical

import android.content.Context
import android.content.SharedPreferences
import android.content.pm.ActivityInfo
import androidx.test.core.app.ActivityScenario
import androidx.test.core.app.ApplicationProvider
import androidx.test.espresso.Espresso.onView
import androidx.test.espresso.IdlingRegistry
import androidx.test.espresso.action.ViewActions.*
import androidx.test.espresso.assertion.ViewAssertions.matches
import androidx.test.espresso.matcher.ViewMatchers.*
import androidx.test.espresso.matcher.ViewMatchers.Visibility
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.filters.LargeTest
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.uiautomator.UiDevice
import android.os.Build
import com.alarm.critical.api.ApiProvider
import com.alarm.critical.model.AlarmResponse
import org.junit.*
import org.junit.Assert.*
import org.junit.runner.RunWith
import org.junit.runners.MethodSorters
import retrofit2.Response

/**
 * Tests E2E Espresso entièrement isolés — aucun backend nécessaire.
 * Utilise FakeApiService + PollingIdlingResource (pas de Thread.sleep sauf timing scénario).
 *
 * Lancer avec : ./gradlew connectedAndroidTest
 */
@RunWith(AndroidJUnit4::class)
@LargeTest
@FixMethodOrder(MethodSorters.NAME_ASCENDING)
class AlarmE2ETest {

    companion object {
        @BeforeClass
        @JvmStatic
        fun checkAppInstalled() {
            val ctx = ApplicationProvider.getApplicationContext<Context>()
            try {
                ctx.packageManager.getPackageInfo("com.alarm.critical", 0)
            } catch (e: Exception) {
                Assume.assumeTrue(
                    "App com.alarm.critical non installée — tous les tests ignorés", false
                )
            }
        }
    }

    private lateinit var context: Context
    private lateinit var fakeApi: FakeApiService
    private lateinit var uiDevice: UiDevice

    private val fakeAlarm = AlarmResponse(
        id = 42,
        title = "Serveur en panne",
        message = "Le serveur de production ne répond plus",
        severity = "critical",
        status = "active",
        assigned_user_id = 1,
        acknowledged_at = null,
        acknowledged_by_name = null,
        suspended_until = null,
        escalation_count = 0,
        created_at = "2026-01-01T00:00:00"
    )

    private val fakeAckedAlarm = fakeAlarm.copy(
        status = "acknowledged",
        acknowledged_at = "2026-01-01T00:01:00",
        acknowledged_by_name = "user1",
        suspended_until = "2026-01-01T00:31:00"
    )

    private val fakeResolvedAlarm = fakeAlarm.copy(
        id = 41,
        title = "Disque plein",
        message = "Espace disque critique",
        status = "resolved",
        created_at = "2025-12-31T10:00:00",
        acknowledged_at = "2025-12-31T10:05:00",
        acknowledged_by_name = "user1"
    )

    private fun launchDashboard(): ActivityScenario<DashboardActivity> {
        val intent = android.content.Intent(context, DashboardActivity::class.java).apply {
            putExtra("token", "fake-token")
        }
        return ActivityScenario.launch(intent)
    }

    private fun waitForPolls(n: Int): PollingIdlingResource {
        val idling = PollingIdlingResource("poll-wait-$n-${System.nanoTime()}", n)
        fakeApi.pollingIdlingResource = idling
        IdlingRegistry.getInstance().register(idling)
        return idling
    }

    private fun waitForHeartbeats(n: Int): PollingIdlingResource {
        val idling = PollingIdlingResource("hb-wait-$n-${System.nanoTime()}", n)
        fakeApi.heartbeatIdlingResource = idling
        IdlingRegistry.getInstance().register(idling)
        return idling
    }

    private fun unregisterAll() {
        IdlingRegistry.getInstance().resources.forEach {
            IdlingRegistry.getInstance().unregister(it)
        }
        fakeApi.pollingIdlingResource = null
        fakeApi.heartbeatIdlingResource = null
    }

    @Before
    fun setUp() {
        context = ApplicationProvider.getApplicationContext()
        uiDevice = UiDevice.getInstance(InstrumentationRegistry.getInstrumentation())

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            InstrumentationRegistry.getInstrumentation().uiAutomation
                .grantRuntimePermission(
                    "com.alarm.critical",
                    "android.permission.POST_NOTIFICATIONS"
                )
        }

        fakeApi = FakeApiService()
        ApiProvider.override(fakeApi)

        com.alarm.critical.service.AlarmPollingService.heartbeatLostSince = 0L
        com.alarm.critical.service.AlarmPollingService.heartbeatLostAlarm = false
        com.alarm.critical.service.AlarmPollingService.lastHeartbeatOk = false
        com.alarm.critical.service.AlarmPollingService.heartbeatLossTimeoutMs = 120_000L
        com.alarm.critical.service.AlarmPollingService.currentAlarm = null
        com.alarm.critical.service.AlarmPollingService.authErrorAlarm = false
        com.alarm.critical.service.AlarmPollingService.authErrorMessage = null
        com.alarm.critical.service.AlarmPollingService.tokenRefreshIntervalMs = 12 * 60 * 60 * 1000L

        val prefs: SharedPreferences = context.getSharedPreferences("alarm_prefs", Context.MODE_PRIVATE)
        prefs.edit()
            .putString("token", "fake-token")
            .putString("user_name", "user1")
            .putInt("user_id", 1)
            .putString("device_token", "test-device")
            .commit()

        // Forcer orientation portrait pour les tests
        uiDevice.setOrientationNatural()
    }

    @After
    fun tearDown() {
        unregisterAll()
        ApiProvider.reset()
        context.getSharedPreferences("alarm_prefs", Context.MODE_PRIVATE)
            .edit().clear().commit()
        uiDevice.setOrientationNatural()
    }

    // ── 01. Pas d'alarme → ligne inactive ─────────────────────────────────

    @Test
    fun test01_noAlarmShowsInactiveLine() {
        fakeApi.myAlarmsResponses = mutableListOf(Response.success(emptyList()))
        waitForPolls(2)

        val scenario = launchDashboard()

        onView(withId(R.id.currentAlarmLine))
            .check(matches(isDisplayed()))
        onView(withId(R.id.currentAlarmLine))
            .check(matches(withSubstring("\u26AA")))
        onView(withId(R.id.dashboardAckButton))
            .check(matches(withEffectiveVisibility(Visibility.GONE)))

        scenario.close()
    }

    // ── 02. Alarme active avec durée ──────────────────────────────────────

    @Test
    fun test02_activeAlarmShowsOnDashboardWithDuration() {
        fakeApi.myAlarmsResponses = mutableListOf(Response.success(listOf(fakeAlarm)))
        waitForPolls(2)

        val scenario = launchDashboard()

        onView(withId(R.id.currentAlarmLine))
            .check(matches(withSubstring("\uD83D\uDD34")))
        onView(withId(R.id.alarmTitle))
            .check(matches(isDisplayed()))
        onView(withId(R.id.alarmTitle))
            .check(matches(withText("Serveur en panne")))
        onView(withId(R.id.alarmDuration))
            .check(matches(withSubstring("depuis")))
        onView(withId(R.id.dashboardAckButton))
            .check(matches(withEffectiveVisibility(Visibility.VISIBLE)))

        scenario.close()
    }

    // ── 03. Acquitter affiche statut + temps restant ──────────────────────

    @Test
    fun test03_acknowledgeShowsStatusAndRemainingTime() {
        fakeApi.myAlarmsResponses = mutableListOf(Response.success(listOf(fakeAlarm)))
        waitForPolls(2)

        val scenario = launchDashboard()

        onView(withId(R.id.dashboardAckButton))
            .check(matches(withEffectiveVisibility(Visibility.VISIBLE)))

        unregisterAll()

        onView(withId(R.id.dashboardAckButton)).perform(click())

        fakeApi.resetCallCount()
        waitForPolls(1)

        onView(withId(R.id.ackStatusText))
            .check(matches(isDisplayed()))
        onView(withId(R.id.ackStatusText))
            .check(matches(withSubstring("Acquittée")))
        onView(withId(R.id.ackRemainingTime))
            .check(matches(isDisplayed()))
        onView(withId(R.id.ackRemainingTime))
            .check(matches(withSubstring("min")))

        assertNotNull("acknowledgeAlarm non appelé", fakeApi.acknowledgeCalledWith)
        assertEquals(42, fakeApi.acknowledgeCalledWith!!.second)

        scenario.close()
    }

    // ── 04. Login depuis l'état déconnecté ────────────────────────────────

    @Test
    fun test04_loginFromLoggedOutState() {
        context.getSharedPreferences("alarm_prefs", Context.MODE_PRIVATE)
            .edit().clear().commit()

        val scenario = ActivityScenario.launch(MainActivity::class.java)

        onView(withId(R.id.loginButton)).check(matches(isDisplayed()))
        onView(withId(R.id.nameInput))
            .perform(clearText(), typeText("user1"), closeSoftKeyboard())
        onView(withId(R.id.passwordInput))
            .perform(clearText(), typeText("user123"), closeSoftKeyboard())
        onView(withId(R.id.loginButton)).perform(click())

        fakeApi.resetCallCount()
        waitForPolls(1)

        assertTrue("login() non appelé", fakeApi.loginCalled)
        val prefs = context.getSharedPreferences("alarm_prefs", Context.MODE_PRIVATE)
        assertEquals("fake-token", prefs.getString("token", null))

        scenario.close()
    }

    // ── 05. Heartbeat OK → ✅ ─────────────────────────────────────────────

    @Test
    fun test05_heartbeatOkShowsGreenIcon() {
        fakeApi.heartbeatResponse = Response.success(
            com.alarm.critical.model.HeartbeatResponse("ok", "2026-01-01T00:00:00")
        )
        waitForHeartbeats(2)

        val scenario = launchDashboard()

        onView(withId(R.id.connectionStatus))
            .check(matches(withSubstring("\u2705")))

        scenario.close()
    }

    // ── 06. Heartbeat KO → ❌ ─────────────────────────────────────────────

    @Test
    fun test06_heartbeatFailShowsRedIcon() {
        fakeApi.heartbeatResponse = Response.error(
            500, okhttp3.ResponseBody.create(null, "error")
        )
        waitForHeartbeats(2)

        val scenario = launchDashboard()

        onView(withId(R.id.connectionStatus))
            .check(matches(withSubstring("\u274C")))

        scenario.close()
    }

    // ── 07. Historique des alarmes passées ─────────────────────────────────

    @Test
    fun test07_alarmHistoryShowsPastAlarms() {
        fakeApi.myAlarmsResponses = mutableListOf(Response.success(emptyList()))
        fakeApi.alarmHistoryResponse = Response.success(listOf(fakeResolvedAlarm))
        waitForPolls(2)

        val scenario = launchDashboard()

        onView(withId(R.id.alarmHistorySection))
            .check(matches(isDisplayed()))
        onView(withId(R.id.alarmHistoryList))
            .check(matches(hasDescendant(withSubstring("Disque plein"))))

        scenario.close()
    }

    // ── 08. Alarme terminée → retour icône inactive ───────────────────────

    @Test
    fun test08_alarmEndedShowsOnScreen() {
        fakeApi.myAlarmsResponses = mutableListOf(
            Response.success(listOf(fakeAlarm)),
            Response.success(listOf(fakeAlarm)),
            Response.success(emptyList())
        )
        waitForPolls(4)

        val scenario = launchDashboard()

        onView(withId(R.id.currentAlarmLine))
            .check(matches(withSubstring("\u26AA")))

        scenario.close()
    }

    // ── 09. Perte heartbeat > timeout → alerte connexion ──────────────────

    @Test
    fun test09_heartbeatLossTriggersAlarmAfterTimeout() {
        com.alarm.critical.service.AlarmPollingService.heartbeatLossTimeoutMs = 3000L

        fakeApi.heartbeatResponse = Response.error(
            500, okhttp3.ResponseBody.create(null, "error")
        )
        waitForHeartbeats(4)

        val scenario = launchDashboard()

        onView(withId(R.id.connectionLostAlert))
            .check(matches(isDisplayed()))
        onView(withId(R.id.connectionLostAlert))
            .check(matches(withSubstring("Connexion perdue")))

        com.alarm.critical.service.AlarmPollingService.heartbeatLossTimeoutMs = 120_000L
        scenario.close()
    }

    // ── 10. Heartbeat revient avant timeout → pas d'alerte ────────────────

    @Test
    fun test10_heartbeatRecoveryBeforeTimeoutNoAlarm() {
        com.alarm.critical.service.AlarmPollingService.heartbeatLossTimeoutMs = 15000L

        fakeApi.heartbeatResponse = Response.error(500, okhttp3.ResponseBody.create(null, "err"))

        // Après 2 heartbeats fail (~6s), rétablir
        Thread {
            Thread.sleep(6000)
            fakeApi.heartbeatResponse = Response.success(
                com.alarm.critical.model.HeartbeatResponse("ok", "2026-01-01T00:00:00")
            )
        }.start()

        waitForHeartbeats(8)

        val scenario = launchDashboard()

        onView(withId(R.id.connectionLostAlert))
            .check(matches(withEffectiveVisibility(Visibility.GONE)))

        com.alarm.critical.service.AlarmPollingService.heartbeatLossTimeoutMs = 120_000L
        scenario.close()
    }

    // ── 11. Alarme resonne après expiration ack ───────────────────────────

    @Test
    fun test11_alarmResoundsAfterAckExpiry() {
        fakeApi.myAlarmsResponses = mutableListOf(
            Response.success(listOf(fakeAlarm)),
            Response.success(listOf(fakeAlarm)),
            Response.success(listOf(fakeAlarm))
        )
        waitForPolls(2)
        val scenario = launchDashboard()

        onView(withId(R.id.dashboardAckButton))
            .check(matches(withEffectiveVisibility(Visibility.VISIBLE)))
        unregisterAll()

        onView(withId(R.id.dashboardAckButton)).perform(click())

        // Phase 2 : suspension
        fakeApi.myAlarmsResponses = mutableListOf(
            Response.success(emptyList()),
            Response.success(emptyList()),
            Response.success(emptyList())
        )
        fakeApi.resetCallCount()
        waitForPolls(3)

        onView(withId(R.id.currentAlarmLine))
            .check(matches(withSubstring("\u26AA")))
        unregisterAll()

        // Phase 3 : alarme revient
        fakeApi.myAlarmsResponses = mutableListOf(Response.success(listOf(fakeAlarm)))
        fakeApi.resetCallCount()
        waitForPolls(2)

        onView(withId(R.id.currentAlarmLine))
            .check(matches(withSubstring("\uD83D\uDD34")))
        onView(withId(R.id.dashboardAckButton))
            .check(matches(withEffectiveVisibility(Visibility.VISIBLE)))

        scenario.close()
    }

    // ── 12. Alarme ne resonne PAS si résolue pendant l'ack ────────────────

    @Test
    fun test12_alarmDoesNotResoundIfResolvedDuringAck() {
        fakeApi.myAlarmsResponses = mutableListOf(
            Response.success(listOf(fakeAlarm)),
            Response.success(listOf(fakeAlarm))
        )
        waitForPolls(2)
        val scenario = launchDashboard()

        onView(withId(R.id.dashboardAckButton))
            .check(matches(withEffectiveVisibility(Visibility.VISIBLE)))
        unregisterAll()

        onView(withId(R.id.dashboardAckButton)).perform(click())

        fakeApi.myAlarmsResponses = mutableListOf(Response.success(emptyList()))
        fakeApi.resetCallCount()
        waitForPolls(3)

        onView(withId(R.id.currentAlarmLine))
            .check(matches(withSubstring("\u26AA")))
        onView(withId(R.id.dashboardAckButton))
            .check(matches(withEffectiveVisibility(Visibility.GONE)))

        scenario.close()
    }

    // ── 13. Rotation bloquée — l'état est préservé ────────────────────────

    @Test
    fun test13_rotationIsBlockedAndStatePreserved() {
        fakeApi.myAlarmsResponses = mutableListOf(Response.success(listOf(fakeAlarm)))
        waitForPolls(2)

        val scenario = launchDashboard()

        // Vérifier l'alarme visible en portrait
        onView(withId(R.id.alarmTitle))
            .check(matches(withText("Serveur en panne")))

        unregisterAll()

        // Forcer rotation landscape via UiAutomator
        uiDevice.setOrientationLeft()
        // Petit délai pour laisser le système réagir
        Thread.sleep(1500)

        // L'orientation est verrouillée en portrait → l'alarme doit toujours être visible
        // (pas de recréation d'activité, pas de perte d'état)
        scenario.onActivity { activity ->
            assertEquals(
                "L'activité devrait être verrouillée en portrait",
                ActivityInfo.SCREEN_ORIENTATION_PORTRAIT,
                activity.requestedOrientation
            )
        }

        // Le contenu est toujours là
        fakeApi.resetCallCount()
        waitForPolls(1)

        onView(withId(R.id.alarmTitle))
            .check(matches(withText("Serveur en panne")))
        onView(withId(R.id.dashboardAckButton))
            .check(matches(withEffectiveVisibility(Visibility.VISIBLE)))

        uiDevice.setOrientationNatural()
        scenario.close()
    }

    // ── 14. Déconnexion puis reconnexion ──────────────────────────────────

    @Test
    fun test14_logoutThenReloginWorks() {
        // Phase 1 : dashboard connecté
        fakeApi.myAlarmsResponses = mutableListOf(Response.success(emptyList()))
        waitForPolls(2)

        val scenario = launchDashboard()

        onView(withId(R.id.userNameText))
            .check(matches(withText("user1")))

        unregisterAll()

        // Cliquer déconnexion
        onView(withId(R.id.logoutButton)).perform(click())

        // On est sur l'écran de login
        onView(withId(R.id.loginButton)).check(matches(isDisplayed()))

        // Le token est effacé
        val prefs = context.getSharedPreferences("alarm_prefs", Context.MODE_PRIVATE)
        assertNull("Token devrait être effacé après logout", prefs.getString("token", null))

        // Phase 2 : re-login
        fakeApi.loginCalled = false
        onView(withId(R.id.nameInput))
            .perform(clearText(), typeText("user1"), closeSoftKeyboard())
        onView(withId(R.id.passwordInput))
            .perform(clearText(), typeText("user123"), closeSoftKeyboard())
        onView(withId(R.id.loginButton)).perform(click())

        // Attendre que le polling reprenne après re-login
        fakeApi.resetCallCount()
        waitForPolls(1)

        assertTrue("login() non appelé au re-login", fakeApi.loginCalled)

        // Le token est restauré
        assertEquals("fake-token", prefs.getString("token", null))

        scenario.close()
    }

    // ── 15. App en arrière-plan → alarme captée au retour ─────────────────

    @Test
    fun test15_alarmReceivedWhileInBackground() {
        // Pas d'alarme au départ
        fakeApi.myAlarmsResponses = mutableListOf(Response.success(emptyList()))
        waitForPolls(2)

        val scenario = launchDashboard()

        onView(withId(R.id.currentAlarmLine))
            .check(matches(withSubstring("\u26AA")))

        unregisterAll()

        // Envoyer l'app en arrière-plan
        uiDevice.pressHome()
        Thread.sleep(2000)

        // Pendant que l'app est en background, une alarme arrive
        fakeApi.myAlarmsResponses = mutableListOf(Response.success(listOf(fakeAlarm)))
        fakeApi.resetCallCount()

        // Le service foreground continue de poll en background → il capte l'alarme
        // Attendre quelques polls
        Thread.sleep(5000)

        // Revenir à l'app
        uiDevice.pressRecentApps()
        Thread.sleep(1000)
        // Cliquer sur l'app dans les récents
        val appEntry = uiDevice.findObject(
            androidx.test.uiautomator.UiSelector().descriptionContains("Alarme Critique")
                .className("android.widget.FrameLayout")
        )
        if (appEntry.exists()) {
            appEntry.click()
        } else {
            // Fallback : relancer via l'intent
            val intent = android.content.Intent(context, DashboardActivity::class.java).apply {
                putExtra("token", "fake-token")
                flags = android.content.Intent.FLAG_ACTIVITY_NEW_TASK
            }
            context.startActivity(intent)
        }
        Thread.sleep(2000)

        // L'alarme doit être affichée au retour
        waitForPolls(1)

        onView(withId(R.id.currentAlarmLine))
            .check(matches(withSubstring("\uD83D\uDD34")))
        onView(withId(R.id.alarmTitle))
            .check(matches(withText("Serveur en panne")))

        scenario.close()
    }

    // ── 16. Refresh token fonctionne côté mobile ─────────────────────────────

    @Test
    fun test16_refreshTokenIsCalledAndWorks() {
        // Configurer le fake pour que le refresh retourne un nouveau token
        fakeApi.loginResponse = Response.success(
            com.alarm.critical.model.TokenResponse(
                access_token = "refreshed-token",
                token_type = "bearer",
                user = com.alarm.critical.model.UserResponse(1, "user1@alarm.local", "user1", false, true)
            )
        )
        // Raccourcir l'intervalle de refresh pour le test (3s)
        com.alarm.critical.service.AlarmPollingService.tokenRefreshIntervalMs = 3000L

        fakeApi.myAlarmsResponses = mutableListOf(Response.success(emptyList()))
        waitForPolls(2)

        val scenario = launchDashboard()

        // Attendre que le refresh automatique s'exécute (3s + marge)
        Thread.sleep(6000)

        // Vérifier que le token a été mis à jour dans les prefs
        val prefs = context.getSharedPreferences("alarm_prefs", Context.MODE_PRIVATE)
        assertEquals("refreshed-token", prefs.getString("token", null))

        com.alarm.critical.service.AlarmPollingService.tokenRefreshIntervalMs = 12 * 60 * 60 * 1000L
        scenario.close()
    }

    // ── 17. Échec refresh → sonnerie + message permanent ─────────────────────

    @Test
    fun test17_refreshFailureTriggersAlarmAndShowsMessage() {
        // Configurer le polling pour retourner 401 (token expiré)
        fakeApi.myAlarmsResponses = mutableListOf(
            Response.success(emptyList()),  // Premier poll OK
            Response.error(401, okhttp3.ResponseBody.create(null, "Unauthorized"))  // Ensuite 401
        )
        // Le refresh échoue aussi
        fakeApi.refreshTokenResponse = Response.error(
            401, okhttp3.ResponseBody.create(null, "Unauthorized")
        )

        // Pas d'IdlingResource ici — le polling s'arrête après le 401
        val scenario = launchDashboard()

        // Attendre que le 401 + refresh failure soient détectés
        Thread.sleep(10000)

        // L'alerte auth error doit être visible avec un message compréhensible
        assertTrue(
            "authErrorAlarm devrait être true",
            com.alarm.critical.service.AlarmPollingService.authErrorAlarm
        )
        assertNotNull(
            "authErrorMessage ne devrait pas être null",
            com.alarm.critical.service.AlarmPollingService.authErrorMessage
        )

        // Le message doit être affiché dans l'alerte connexion (permanent, pas toast)
        onView(withId(R.id.connectionLostAlert))
            .check(matches(isDisplayed()))
        onView(withId(R.id.connectionLostAlert))
            .check(matches(withSubstring("session")))

        com.alarm.critical.service.AlarmPollingService.authErrorAlarm = false
        com.alarm.critical.service.AlarmPollingService.authErrorMessage = null
        scenario.close()
    }

    // ── 18. Escalade visible : alarme change d'utilisateur assigné ───────────

    @Test
    fun test18_escalatedAlarmShowsOnNewUser() {
        // Simuler une alarme qui passe de user1 (id=1) à user2 (id=2) via escalade
        val alarmUser1 = fakeAlarm.copy(assigned_user_id = 1, escalation_count = 0)
        val alarmUser1Escalated = fakeAlarm.copy(
            assigned_user_id = 2, escalation_count = 1, status = "escalated"
        )

        // D'abord l'alarme est assignée à user1 (nous), puis elle est escaladée (disparaît)
        fakeApi.myAlarmsResponses = mutableListOf(
            Response.success(listOf(alarmUser1)),
            Response.success(listOf(alarmUser1)),
            Response.success(emptyList())  // Alarme escaladée, plus assignée à nous
        )
        waitForPolls(4)

        val scenario = launchDashboard()

        // L'alarme devrait finir par disparaître (escaladée vers quelqu'un d'autre)
        onView(withId(R.id.currentAlarmLine))
            .check(matches(withSubstring("\u26AA")))  // ⚪ inactive — plus notre alarme

        scenario.close()
    }

    // ── 19. Alarme acquittée par un autre utilisateur ────────────────────────

    @Test
    fun test19_ackedByOtherUserShowsStatusNoSoundNoButton() {
        // Alarme acquittée par user2, nous sommes user1
        val ackedByOther = fakeAlarm.copy(
            status = "acknowledged",
            acknowledged_at = "2026-01-01T00:01:00",
            acknowledged_by_name = "user2",
            suspended_until = "2026-01-01T00:31:00",
            ack_remaining_seconds = 1500
        )

        fakeApi.myAlarmsResponses = mutableListOf(
            Response.success(listOf(ackedByOther)),
            Response.success(listOf(ackedByOther)),
            Response.success(listOf(ackedByOther))
        )
        waitForPolls(2)

        val scenario = launchDashboard()

        // Titre visible
        onView(withId(R.id.alarmTitle))
            .check(matches(withText("Serveur en panne")))

        // Status indique acquittée par user2
        onView(withId(R.id.ackStatusText))
            .check(matches(withEffectiveVisibility(Visibility.VISIBLE)))
        onView(withId(R.id.ackStatusText))
            .check(matches(withSubstring("user2")))

        // Bouton acquitter masqué
        onView(withId(R.id.dashboardAckButton))
            .check(matches(withEffectiveVisibility(Visibility.GONE)))

        // Temps restant visible
        onView(withId(R.id.ackRemainingTime))
            .check(matches(withEffectiveVisibility(Visibility.VISIBLE)))
        onView(withId(R.id.ackRemainingTime))
            .check(matches(withSubstring("min")))

        scenario.close()
    }

    // ── 20. Countdown se met à jour à chaque poll ────────────────────────────

    @Test
    fun test20_ackCountdownUpdatesOnEachPoll() {
        // Phase 1 : alarme active
        fakeApi.myAlarmsResponses = mutableListOf(
            Response.success(listOf(fakeAlarm)),
            Response.success(listOf(fakeAlarm))
        )
        waitForPolls(2)

        val scenario = launchDashboard()

        onView(withId(R.id.dashboardAckButton))
            .check(matches(withEffectiveVisibility(Visibility.VISIBLE)))
        unregisterAll()

        // User acquitte
        onView(withId(R.id.dashboardAckButton)).perform(click())

        // Phase 2 : alarme acknowledged avec 1800s restantes
        val acked1800 = fakeAlarm.copy(
            status = "acknowledged",
            acknowledged_at = "2026-01-01T00:01:00",
            acknowledged_by_name = "user1",
            suspended_until = "2026-01-01T00:31:00",
            ack_remaining_seconds = 1800
        )
        fakeApi.myAlarmsResponses = mutableListOf(
            Response.success(listOf(acked1800)),
            Response.success(listOf(acked1800))
        )
        fakeApi.resetCallCount()
        waitForPolls(2)

        onView(withId(R.id.ackRemainingTime))
            .check(matches(withSubstring("30 min")))
        unregisterAll()

        // Phase 3 : après un moment, 1740s restantes (29 min)
        val acked1740 = acked1800.copy(ack_remaining_seconds = 1740)
        fakeApi.myAlarmsResponses = mutableListOf(
            Response.success(listOf(acked1740)),
            Response.success(listOf(acked1740))
        )
        fakeApi.resetCallCount()
        waitForPolls(2)

        onView(withId(R.id.ackRemainingTime))
            .check(matches(withSubstring("29 min")))

        scenario.close()
    }

    // ── 21. Nouvelle alarme après résolution reset l'ack et sonne ────────────

    @Test
    fun test21_newAlarmAfterResolvedResetsAckAndRings() {
        // Phase 1 : alarme active id=42
        fakeApi.myAlarmsResponses = mutableListOf(
            Response.success(listOf(fakeAlarm)),
            Response.success(listOf(fakeAlarm))
        )
        waitForPolls(2)

        val scenario = launchDashboard()

        onView(withId(R.id.dashboardAckButton))
            .check(matches(withEffectiveVisibility(Visibility.VISIBLE)))
        unregisterAll()

        // User acquitte
        onView(withId(R.id.dashboardAckButton)).perform(click())

        // Phase 2 : alarme acknowledged puis résolue (disparaît)
        val ackedAlarm = fakeAlarm.copy(
            status = "acknowledged",
            acknowledged_at = "2026-01-01T00:01:00",
            acknowledged_by_name = "user1",
            suspended_until = "2026-01-01T00:31:00",
            ack_remaining_seconds = 1800
        )
        fakeApi.myAlarmsResponses = mutableListOf(
            Response.success(listOf(ackedAlarm)),
            Response.success(emptyList()),  // Alarme résolue
            Response.success(emptyList())
        )
        fakeApi.resetCallCount()
        waitForPolls(3)

        onView(withId(R.id.currentAlarmLine))
            .check(matches(withSubstring("\u26AA")))  // ⚪ inactive
        unregisterAll()

        // Phase 3 : NOUVELLE alarme id=99
        val newAlarm = AlarmResponse(
            id = 99,
            title = "Nouvelle alarme",
            message = "Un nouveau problème",
            severity = "critical",
            status = "active",
            assigned_user_id = 1,
            acknowledged_at = null,
            acknowledged_by_name = null,
            suspended_until = null,
            escalation_count = 0,
            created_at = "2026-01-01T01:00:00"
        )
        fakeApi.myAlarmsResponses = mutableListOf(
            Response.success(listOf(newAlarm)),
            Response.success(listOf(newAlarm))
        )
        fakeApi.resetCallCount()
        waitForPolls(2)

        // Le bouton ack doit être visible (l'ack précédent est reset car ID différent)
        onView(withId(R.id.dashboardAckButton))
            .check(matches(withEffectiveVisibility(Visibility.VISIBLE)))
        onView(withId(R.id.alarmTitle))
            .check(matches(withText("Nouvelle alarme")))
        onView(withId(R.id.currentAlarmLine))
            .check(matches(withSubstring("\uD83D\uDD34")))  // 🔴 active

        scenario.close()
    }
}
