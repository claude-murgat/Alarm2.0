package com.alarm.critical

import android.app.Activity
import android.app.Instrumentation
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.SystemClock
import android.view.View
import androidx.core.view.GravityCompat
import androidx.drawerlayout.widget.DrawerLayout
import androidx.lifecycle.Lifecycle
import androidx.test.core.app.ActivityScenario
import androidx.test.espresso.Espresso.onView
import androidx.test.espresso.action.ViewActions.clearText
import androidx.test.espresso.action.ViewActions.click
import androidx.test.espresso.action.ViewActions.closeSoftKeyboard
import androidx.test.espresso.action.ViewActions.replaceText
import androidx.test.espresso.action.ViewActions.scrollTo
import androidx.test.espresso.action.ViewActions.typeText
import androidx.test.espresso.assertion.ViewAssertions.doesNotExist
import androidx.test.espresso.assertion.ViewAssertions.matches
import androidx.test.espresso.intent.Intents
import androidx.test.espresso.intent.matcher.IntentMatchers.anyIntent
import androidx.test.espresso.intent.matcher.IntentMatchers.hasComponent
import androidx.test.espresso.matcher.RootMatchers.isDialog
import androidx.test.espresso.matcher.ViewMatchers.Visibility
import androidx.test.espresso.matcher.ViewMatchers.isDisplayed
import androidx.test.espresso.matcher.ViewMatchers.withContentDescription
import androidx.test.espresso.matcher.ViewMatchers.withEffectiveVisibility
import androidx.test.espresso.matcher.ViewMatchers.withHint
import androidx.test.espresso.matcher.ViewMatchers.withId
import androidx.test.espresso.matcher.ViewMatchers.withText
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.runner.lifecycle.ActivityLifecycleMonitorRegistry
import androidx.test.runner.lifecycle.Stage
import com.alarm.critical.api.ApiProvider
import com.alarm.critical.model.AlarmResponse
import com.alarm.critical.model.EscalationEntry
import com.alarm.critical.model.ModemStatusResponse
import com.alarm.critical.model.PhoneUpdate
import com.alarm.critical.model.TokenResponse
import com.alarm.critical.model.UserCreateRequest
import com.alarm.critical.model.UserResponse
import com.alarm.critical.service.AlarmPollingService
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import retrofit2.Response

/**
 * INV-ANDROID-010 : role admin memorise au login, relu par /api/auth/me, efface au logout.
 * INV-ANDROID-011 : menu lateral (entrees admin masquees aux autres) et ses 6 ecrans ;
 *                   un 403 du serveur ferme l'ecran admin.
 * INV-ANDROID-012 : version de l'app et noeud qui repond en bas du menu.
 */
@RunWith(AndroidJUnit4::class)
class AlarmMenuAdminTest {

    private lateinit var context: Context
    private lateinit var fakeApi: FakeApiService
    private val scenarios = mutableListOf<ActivityScenario<*>>()

    private val everyone = listOf(R.id.drawerHome, R.id.drawerAlarms, R.id.drawerEscalation, R.id.drawerModems)
    private val adminOnly = listOf(R.id.drawerTeam, R.id.drawerChain, R.id.drawerSupervision)

    private fun prefs() = context.getSharedPreferences("alarm_prefs", Context.MODE_PRIVATE)

    private fun waitFor(timeoutMs: Long = 15000, cond: () -> Boolean): Boolean {
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
        val ui = InstrumentationRegistry.getInstrumentation().uiAutomation
        ui.grantRuntimePermission(context.packageName, "android.permission.READ_PHONE_STATE")
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            ui.grantRuntimePermission(context.packageName, "android.permission.POST_NOTIFICATIONS")
        }
        fakeApi = FakeApiService()
        ApiProvider.override(fakeApi)
        prefs().edit().clear()
            .putString("token", "fake-token")
            .putString("user_name", "user1")
            .putInt("user_id", 1)
            .putBoolean("battery_optimization_asked", true)
            .putBoolean("fsi_permission_asked", true)
            .commit()
    }

    @After
    fun tearDown() {
        scenarios.reversed().forEach { it.close() }
        // Ecrans ouverts depuis le menu (hors scenario) : on les ferme aussi
        InstrumentationRegistry.getInstrumentation().runOnMainSync {
            val monitor = ActivityLifecycleMonitorRegistry.getInstance()
            listOf(Stage.RESUMED, Stage.PAUSED, Stage.STOPPED)
                .flatMap { monitor.getActivitiesInStage(it) }
                .forEach { it.finish() }
        }
        context.stopService(Intent(context, AlarmPollingService::class.java))
        waitFor { !AlarmPollingService.isRunning }
        ApiProvider.reset()
        prefs().edit().clear().commit()
    }

    /** Dashboard avec le role memorise au login ([saved]) et celui que renvoie /me ([server]). */
    private fun launchDashboard(saved: Boolean, server: Boolean = saved): ActivityScenario<DashboardActivity> {
        prefs().edit().putBoolean("is_admin", saved).commit()
        fakeApi.meResponse = Response.success(UserResponse(1, "", "user1", server, true))
        return ActivityScenario.launch<DashboardActivity>(
            Intent(context, DashboardActivity::class.java).putExtra("token", "fake-token")
        ).also { scenarios += it }
    }

    /** Ouvre le tiroir sans animation : un tiroir ferme est INVISIBLE. */
    private fun ActivityScenario<DashboardActivity>.openDrawer() = onActivity {
        it.findViewById<DrawerLayout>(R.id.drawerLayout).openDrawer(GravityCompat.START, false)
    }

    private fun <A : Activity> launch(screen: Class<A>) = ActivityScenario.launch(screen).also { scenarios += it }

    private fun visible(id: Int) = onView(withId(id)).check(matches(withEffectiveVisibility(Visibility.VISIBLE)))
    private fun gone(id: Int) = onView(withId(id)).check(matches(withEffectiveVisibility(Visibility.GONE)))
    private fun shown(text: String) = onView(withText(text)).perform(scrollTo()).check(matches(isDisplayed()))

    private fun alarm(id: Int, site: String?, title: String) = AlarmResponse(
        id, title, "msg", "critical", "resolved", 1, null, null, null, 0, "2026-10-28T08:00:00", site = site
    )

    // ===== INV-ANDROID-010 / 011 : gating du menu =====

    @Test
    fun non_admin_ne_voit_pas_les_entrees_admin() {
        launchDashboard(saved = false).openDrawer()

        everyone.forEach { visible(it) }
        adminOnly.forEach { gone(it) }
    }

    @Test
    fun admin_voit_les_entrees_admin() {
        launchDashboard(saved = true).openDrawer()

        (everyone + adminOnly).forEach { visible(it) }
    }

    @Test
    fun role_admin_relu_au_demarrage_du_dashboard() {
        // Promu admin depuis le login : /me fait foi
        launchDashboard(saved = false, server = true).openDrawer()

        assertTrue("is_admin relu depuis /api/auth/me", waitFor { prefs().getBoolean("is_admin", false) })
        adminOnly.forEach { visible(it) }
    }

    @Test
    fun login_memorise_is_admin() {
        prefs().edit().remove("token").commit()  // sinon MainActivity file au dashboard
        fakeApi.loginResponse = Response.success(TokenResponse(
            access_token = "fake-token", token_type = "bearer",
            user = UserResponse(3, "", "admin", true, true)
        ))
        // /me KO : le dashboard ouvert apres le login ne reecrit pas le role
        fakeApi.meResponse = Response.error(500, okhttp3.ResponseBody.create(null, "err"))
        scenarios += ActivityScenario.launch(MainActivity::class.java)

        onView(withId(R.id.nameInput)).perform(clearText(), typeText("admin"), closeSoftKeyboard())
        onView(withId(R.id.passwordInput)).perform(clearText(), typeText("admin123"), closeSoftKeyboard())
        onView(withId(R.id.loginButton)).perform(click())

        assertTrue("le role admin doit etre memorise au login", waitFor { prefs().getBoolean("is_admin", false) })
    }

    @Test
    fun logout_efface_is_admin() {
        val dashboard = launchDashboard(saved = true)

        dashboard.onActivity { it.findViewById<View>(R.id.drawerLogout).performClick() }

        assertTrue("is_admin efface au logout", waitFor { !prefs().contains("is_admin") })
    }

    @Test
    fun chaque_entree_du_menu_ouvre_son_ecran() {
        val dashboard = launchDashboard(saved = true)
        val screens = mapOf(
            R.id.drawerAlarms to AlarmsActivity::class.java,
            R.id.drawerEscalation to EscalationActivity::class.java,
            R.id.drawerModems to ModemsActivity::class.java,
            R.id.drawerTeam to TeamActivity::class.java,
            R.id.drawerChain to ChainActivity::class.java,
            R.id.drawerSupervision to SupervisionActivity::class.java,
        )
        Intents.init()
        try {
            Intents.intending(anyIntent()).respondWith(Instrumentation.ActivityResult(Activity.RESULT_OK, null))
            screens.forEach { (entry, screen) ->
                dashboard.onActivity { it.findViewById<View>(entry).performClick() }
                Intents.intended(hasComponent(screen.name))
            }
        } finally {
            Intents.release()
        }
    }

    // ===== INV-ANDROID-012 : version et noeud =====

    @Test
    fun version_et_noeud_en_bas_du_menu() {
        launchDashboard(saved = false)

        onView(withId(R.id.drawerVersion))
            .check(matches(withText("Version ${BuildConfig.VERSION_NAME} · nœud node1 (Izeron)")))
    }

    @Test
    fun noeud_inconnu_affiche_point_d_interrogation() {
        fakeApi.clusterResponse = Response.error(503, okhttp3.ResponseBody.create(null, ""))  // Patroni KO / serveur en erreur
        launchDashboard(saved = false)

        onView(withId(R.id.drawerVersion))
            .check(matches(withText("Version ${BuildConfig.VERSION_NAME} · nœud ?")))
    }

    // ===== INV-ANDROID-011 : ecrans pour tous =====

    @Test
    fun ecran_alarmes_tous_les_sites_filtre_par_site_et_periode() {
        fakeApi.alarmHistoryResponse = Response.success(listOf(
            alarm(1, "izeron", "Contact sec"),
            alarm(2, "beaufort", "Contact sec"),
            alarm(3, null, "Perte heartbeat"),
        ))
        launch(AlarmsActivity::class.java)

        shown("Izeron · Contact sec")
        shown("Beaufort · Contact sec")
        shown("Astreinte · Perte heartbeat")

        onView(withText("Beaufort")).perform(click())
        shown("Beaufort · Contact sec")
        onView(withText("Izeron · Contact sec")).check(doesNotExist())
        onView(withText("Astreinte · Perte heartbeat")).check(doesNotExist())

        onView(withText("90 jours")).perform(click())
        assertTrue("periode 90 jours demandee au serveur", waitFor { fakeApi.lastHistoryDays == 90 })
    }

    @Test
    fun ecran_escalade_alarme_palier_et_chaine() {
        launch(EscalationActivity::class.java)

        shown("Beaufort · Déclenchement contact sec — Beaufort")
        shown("Palier 2 · en attente d'acquittement")
        shown("1. user1")
        shown("De garde · déjà appelé")
        shown("2. user2")
        shown("appelé en ce moment")
        shown("3. admin")
    }

    @Test
    fun ecran_sites_et_modems_une_ligne_par_passerelle() {
        val izeron = ModemStatusResponse("ok", 3, "LTE", -95.0, "2026-10-29T08:00:00", "alarm-1", "izeron")
        val beaufort = ModemStatusResponse("no_network", 0, null, null, "2026-10-29T08:00:00", "alarm-3", "beaufort")
        fakeApi.modemStatusResponse = Response.success(
            beaufort.copy(gateways = listOf(izeron, beaufort))  // premier niveau = la plus mal en point
        )
        launch(ModemsActivity::class.java)

        shown("Izeron (alarm-1)")
        shown("Beaufort (alarm-3)")
        onView(withContentDescription("Signal LTE du modem : 3/4 (-95.0 dBm)")).check(matches(isDisplayed()))
        onView(withContentDescription("Modem sans réseau : SMS et appels de secours impossibles"))
            .check(matches(isDisplayed()))
    }

    // ===== INV-ANDROID-011 : ecrans admin =====

    @Test
    fun ecran_equipe_liste_et_suppression_apres_confirmation() {
        launch(TeamActivity::class.java)

        shown("user2")
        shown("+33622222222")
        shown("pas de téléphone · admin")

        onView(withContentDescription("Supprimer user2")).perform(click())
        assertEquals("rien n'est supprime avant la confirmation", null, fakeApi.deletedUserId)
        onView(withText("Supprimer")).inRoot(isDialog()).perform(click())

        assertTrue("DELETE /api/users/2", waitFor { fakeApi.deletedUserId == 2 })
    }

    @Test
    fun ecran_equipe_creation_et_telephone() {
        launch(TeamActivity::class.java)

        onView(withText("Ajouter un utilisateur")).perform(click())
        onView(withHint("Nom (minuscules, sans espace)")).inRoot(isDialog()).perform(replaceText("user3"))
        onView(withHint("Mot de passe")).inRoot(isDialog()).perform(replaceText("secret12"))
        onView(withHint("Téléphone (facultatif)")).inRoot(isDialog()).perform(replaceText("+33633333333"))
        onView(withText("Compte test (ne sonne pas)")).inRoot(isDialog()).perform(click())
        onView(withText("Créer")).inRoot(isDialog()).perform(click())
        assertTrue("POST /api/auth/register", waitFor {
            fakeApi.registeredUser == UserCreateRequest("user3", "secret12", "+33633333333", false, true)
        })

        onView(withContentDescription("Modifier le téléphone de user2")).perform(click())
        onView(withText("+33622222222")).inRoot(isDialog()).perform(replaceText("+33699999999"))
        onView(withText("Enregistrer")).inRoot(isDialog()).perform(click())
        assertTrue("PATCH /api/users/2", waitFor { fakeApi.phoneUpdate == (2 to PhoneUpdate("+33699999999")) })
    }

    @Test
    fun ecran_chaine_reordonne_enregistre_et_regle_les_delais() {
        fakeApi.escalationChainResponse = Response.success(listOf(
            EscalationEntry(1, 1), EscalationEntry(2, 2), EscalationEntry(3, 3)
        ))
        launch(ChainActivity::class.java)

        shown("1. user1")
        onView(withContentDescription("Descendre user1")).perform(click())
        shown("1. user2")
        shown("2. user1")
        onView(withText("Enregistrer l'ordre")).perform(scrollTo(), click())
        assertTrue("POST bulk [2, 1, 3]", waitFor { fakeApi.savedChain == listOf(2, 1, 3) })

        onView(withText("15")).perform(scrollTo(), replaceText("20"))
        onView(withText("Enregistrer le délai d'escalade")).perform(scrollTo(), click())
        assertTrue("delai d'escalade 20 min", waitFor { fakeApi.escalationDelay == 20.0 })

        onView(withText("2")).perform(scrollTo(), replaceText("3"))
        onView(withText("Enregistrer le délai SMS et appel")).perform(scrollTo(), click())
        assertTrue("delai SMS/appel 3 min", waitFor { fakeApi.smsCallDelay == 3.0 })
    }

    @Test
    fun ecran_supervision_cluster_kpi_et_audit() {
        launch(SupervisionActivity::class.java)

        shown("node1 · Izeron")
        shown("primaire · running")
        shown("node3 · OVH")
        shown("node5 · Beaufort")
        shown("12 alarmes")
        shown("user_login")
    }

    @Test
    fun ecran_admin_ferme_sur_403() {
        fakeApi.auditResponse = Response.error(403, okhttp3.ResponseBody.create(null, """{"detail":"Admin requis"}"""))

        val screen = launch(SupervisionActivity::class.java)

        assertTrue("403 -> l'ecran se ferme", waitFor { screen.state == Lifecycle.State.DESTROYED })
    }
}
