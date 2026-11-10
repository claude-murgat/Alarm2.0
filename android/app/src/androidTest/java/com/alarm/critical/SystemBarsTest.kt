package com.alarm.critical

import android.content.Context
import android.content.Intent
import android.graphics.Color
import android.os.Build
import android.os.SystemClock
import androidx.core.view.GravityCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.drawerlayout.widget.DrawerLayout
import androidx.lifecycle.Lifecycle
import androidx.test.core.app.ActivityScenario
import androidx.test.espresso.Espresso.onView
import androidx.test.espresso.action.ViewActions.click
import androidx.test.espresso.matcher.ViewMatchers.withId
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.runner.lifecycle.ActivityLifecycleMonitorRegistry
import androidx.test.runner.lifecycle.Stage
import androidx.test.uiautomator.UiDevice
import com.alarm.critical.api.ApiProvider
import com.alarm.critical.service.AlarmPollingService
import com.alarm.critical.service.AlarmSoundManager
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

/**
 * INV-ANDROID-708 : cible Android 16 (targetSdk 36). Depuis Android 15 l'app est dessinee sous les barres
 * systeme (edge-to-edge impose) : aucun element cle d'un ecran sous la barre d'etat ou de navigation.
 * Avec le retour predictif d'Android 16, le retour ne ferme toujours pas l'ecran d'alarme (INV-ANDROID-004)
 * et ferme d'abord le menu lateral.
 */
@RunWith(AndroidJUnit4::class)
class SystemBarsTest {

    private lateinit var context: Context
    private val scenarios = mutableListOf<ActivityScenario<*>>()

    private fun prefs() = context.getSharedPreferences("alarm_prefs", Context.MODE_PRIVATE)

    private fun waitFor(timeoutMs: Long = 10_000, cond: () -> Boolean): Boolean {
        val end = SystemClock.uptimeMillis() + timeoutMs
        while (SystemClock.uptimeMillis() < end) {
            if (cond()) return true
            SystemClock.sleep(100)
        }
        return cond()
    }

    /** La vue [id] tient entre la barre d'etat (et l'encoche) en haut et la barre de navigation en bas. */
    private fun clearOfSystemBars(id: Int, name: String) {
        onView(withId(id)).check { v, e ->
            if (v == null) throw e
            val bars = ViewCompat.getRootWindowInsets(v)!!
                .getInsets(WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout())
            val top = IntArray(2).also { v.getLocationInWindow(it) }[1]
            val bottom = top + v.height
            assertTrue("$name : haut ($top px) sous la barre d'etat (${bars.top} px)", top >= bars.top)
            assertTrue("$name : bas ($bottom px) au-dessus de la barre de navigation (${v.rootView.height - bars.bottom} px)",
                bottom <= v.rootView.height - bars.bottom)
        }
    }

    @Before
    fun setUp() {
        context = InstrumentationRegistry.getInstrumentation().targetContext
        val ui = InstrumentationRegistry.getInstrumentation().uiAutomation
        ui.grantRuntimePermission(context.packageName, "android.permission.READ_PHONE_STATE")  // demande de l'ecran de connexion
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            ui.grantRuntimePermission(context.packageName, "android.permission.POST_NOTIFICATIONS")
        }
        ApiProvider.override(FakeApiService())
        prefs().edit().clear().commit()
        AlarmPollingService.alarms = emptyList()
    }

    @After
    fun tearDown() {
        scenarios.reversed().forEach { it.close() }
        InstrumentationRegistry.getInstrumentation().runOnMainSync {
            val monitor = ActivityLifecycleMonitorRegistry.getInstance()
            listOf(Stage.RESUMED, Stage.PAUSED, Stage.STOPPED)
                .flatMap { monitor.getActivitiesInStage(it) }
                .forEach { it.finish() }
        }
        AlarmSoundManager(context).stopAlarmSound()
        AlarmPollingService.alarms = emptyList()
        ApiProvider.reset()
        prefs().edit().clear().commit()
    }

    /** Connecte, hors astreinte (pas de service), demandes « une fois » deja faites. */
    private fun loggedIn() = prefs().edit()
        .putString("token", "fake-token")
        .putString("user_name", "user1")
        .putBoolean("is_oncall", false)
        .putBoolean("is_admin", true)
        .putBoolean("battery_optimization_asked", true)
        .putBoolean("fsi_permission_asked", true)
        .commit()

    private fun launchDashboard(): ActivityScenario<DashboardActivity> {
        loggedIn()
        return ActivityScenario.launch<DashboardActivity>(
            Intent(context, DashboardActivity::class.java).putExtra("token", "fake-token")
        ).also { scenarios += it }
    }

    private fun launchAlarm(): ActivityScenario<AlarmActivity> {
        loggedIn()
        return ActivityScenario.launch<AlarmActivity>(
            Intent(context, AlarmActivity::class.java).putExtra("alarm_id", 42).putExtra("token", "fake-token")
        ).also {
            scenarios += it
            assertTrue("ecran d'alarme affiche", waitFor { AlarmActivity.isVisible })
        }
    }

    @Test
    fun cible_android_16() {
        assertEquals("targetSdk", 36, context.applicationInfo.targetSdkVersion)
    }

    @Test
    fun tableau_de_bord_et_menu_lateral_hors_des_barres_systeme() {
        val dashboard = launchDashboard()
        clearOfSystemBars(R.id.menuButton, "bouton du menu")
        clearOfSystemBars(R.id.helpFab, "bouton d'aide (logs)")

        // Les icones de la barre d'etat sont blanches : le fond dessous doit etre sombre pour qu'on les voie
        var statusBar = 0
        onView(withId(R.id.menuButton)).check { v, _ ->
            statusBar = ViewCompat.getRootWindowInsets(v)!!.getInsets(WindowInsetsCompat.Type.statusBars()).top
        }
        val screen = InstrumentationRegistry.getInstrumentation().uiAutomation.takeScreenshot()
        val under = screen.getPixel(screen.width * 2 / 5, statusBar / 2)  // entre l'heure et l'encoche
        assertTrue("fond sous la barre d'etat sombre : #${Integer.toHexString(under)}", Color.luminance(under) < 0.3f)

        dashboard.onActivity { it.findViewById<DrawerLayout>(R.id.drawerLayout).openDrawer(GravityCompat.START, false) }
        clearOfSystemBars(R.id.drawerInitial, "haut du menu lateral")
        clearOfSystemBars(R.id.drawerLogout, "« Se déconnecter » en bas du menu")
    }

    @Test
    fun ecran_du_menu_hors_des_barres_systeme() {
        loggedIn()
        scenarios += ActivityScenario.launch(UpdateActivity::class.java)  // gabarit commun des ecrans du menu
        clearOfSystemBars(R.id.backButton, "bouton retour")
        clearOfSystemBars(R.id.screenTitle, "titre")
    }

    @Test
    fun connexion_hors_des_barres_systeme() {
        scenarios += ActivityScenario.launch(MainActivity::class.java)  // pas de token : ecran de connexion
        clearOfSystemBars(R.id.nameInput, "nom")
        clearOfSystemBars(R.id.loginButton, "bouton de connexion")
        clearOfSystemBars(R.id.shareLogsButton, "« Envoyer les logs »")
    }

    /** Le champ en cours de saisie reste au-dessus du clavier (Android 15+ : plus rien n'ajuste la fenetre a sa place). */
    @Test
    fun connexion_clavier_ouvert_le_champ_saisi_reste_au_dessus() {
        val device = UiDevice.getInstance(InstrumentationRegistry.getInstrumentation())
        device.executeShellCommand("settings put secure show_ime_with_hard_keyboard 1")  // AVD a clavier materiel
        // Plus petit telephone courant (360 x 640 dp) : c'est la que les champs centres sont le plus pres du clavier
        device.executeShellCommand("wm size 720x1280")
        device.executeShellCommand("wm density 320")
        try {
            scenarios += ActivityScenario.launch(MainActivity::class.java)
            for (id in listOf(R.id.nameInput, R.id.passwordInput)) {
                onView(withId(id)).perform(click())
                var detail = ""
                val ok = waitFor {
                    var visible = false
                    onView(withId(id)).check { v, _ ->
                        val insets = ViewCompat.getRootWindowInsets(v)!!
                        val imeTop = v.rootView.height - insets.getInsets(WindowInsetsCompat.Type.ime()).bottom
                        val bottom = IntArray(2).also { v.getLocationInWindow(it) }[1] + v.height
                        detail = "bas du champ $bottom px, haut du clavier $imeTop px"
                        visible = insets.isVisible(WindowInsetsCompat.Type.ime()) && bottom <= imeTop
                    }
                    visible
                }
                assertTrue("clavier ouvert, champ au-dessus : $detail", ok)
            }
        } finally {
            device.executeShellCommand("wm size reset")
            device.executeShellCommand("wm density reset")
            device.executeShellCommand("settings put secure show_ime_with_hard_keyboard 0")
        }
    }

    @Test
    fun ecran_d_alarme_hors_des_barres_systeme() {
        launchAlarm()
        clearOfSystemBars(R.id.topBar, "barre du haut")
        clearOfSystemBars(R.id.ackButton, "bouton « Acquitter »")
    }

    @Test
    fun retour_ne_ferme_pas_l_ecran_d_alarme_qui_sonne_toujours() {
        val alarm = launchAlarm()
        assertTrue("sonnerie", waitFor { AlarmSoundManager(context).isPlaying() })

        UiDevice.getInstance(InstrumentationRegistry.getInstrumentation()).pressBack()

        assertFalse("le retour ne doit pas fermer l'ecran d'alarme", waitFor(3000) { alarm.state != Lifecycle.State.RESUMED })
        assertTrue("toujours la sonnerie", AlarmSoundManager(context).isPlaying())
    }

    @Test
    fun retour_menu_lateral_ouvert_ferme_le_menu_sans_quitter_le_tableau_de_bord() {
        val dashboard = launchDashboard()
        dashboard.onActivity { it.findViewById<DrawerLayout>(R.id.drawerLayout).openDrawer(GravityCompat.START, false) }

        UiDevice.getInstance(InstrumentationRegistry.getInstrumentation()).pressBack()

        fun drawerOpen(): Boolean {
            var open = false
            dashboard.onActivity { open = it.findViewById<DrawerLayout>(R.id.drawerLayout).isDrawerOpen(GravityCompat.START) }
            return open
        }
        waitFor { dashboard.state != Lifecycle.State.RESUMED || !drawerOpen() }
        assertEquals("tableau de bord toujours affiche", Lifecycle.State.RESUMED, dashboard.state)
        assertFalse("menu ferme", drawerOpen())
    }
}
