package com.alarm.critical

import android.app.Activity
import android.app.Instrumentation
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.ParcelFileDescriptor
import android.os.PowerManager
import android.os.SystemClock
import android.provider.Settings
import android.view.accessibility.AccessibilityEvent
import androidx.lifecycle.Lifecycle
import androidx.test.core.app.ActivityScenario
import androidx.test.espresso.Espresso.onView
import androidx.test.espresso.action.ViewActions.click
import androidx.test.espresso.action.ViewActions.scrollTo
import androidx.test.espresso.assertion.ViewAssertions.matches
import androidx.test.espresso.intent.Intents
import androidx.test.espresso.intent.Intents.intended
import androidx.test.espresso.intent.Intents.intending
import androidx.test.espresso.intent.VerificationModes.times
import androidx.test.espresso.intent.matcher.IntentMatchers.hasAction
import androidx.test.espresso.intent.matcher.IntentMatchers.hasComponent
import androidx.test.espresso.matcher.ViewMatchers.withId
import androidx.test.espresso.matcher.ViewMatchers.withText
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.uiautomator.UiDevice
import com.alarm.critical.api.ApiProvider
import com.alarm.critical.model.AlarmResponse
import com.alarm.critical.model.HealthResponse
import com.alarm.critical.service.AlarmPollingService
import com.alarm.critical.service.AlarmSoundManager
import com.alarm.critical.update.PlayProtect
import com.alarm.critical.update.UpdateProvider
import com.alarm.critical.util.AppLogger
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import retrofit2.Response
import java.util.concurrent.TimeoutException

/**
 * INV-ANDROID-707 : reglage Play Protect ouvert de lui-meme une fois par installation (release, jamais pendant
 * une alarme, une seule demande « une fois » par retour au premier plan), et par le bouton de l'ecran « Mise à jour ».
 * Ecrans de reglages stubbes par Espresso-Intents ; build release simule par UpdateProvider.override (INV-ANDROID-705).
 */
@RunWith(AndroidJUnit4::class)
class PlayProtectTest {

    companion object {
        val PLAY_PROTECT = ComponentName("com.google.android.gms",
            "com.google.android.gms.security.settings.VerifyAppsSettingsActivity")
        const val TOAST = "Activez « Améliorer la détection des applis malveillantes » : " +
            "les mises à jour s'installeront sans fenêtre Play Protect"
        // Ecran absent du telephone (GMS ancien, modifie ou absent)
        val ABSENT = ComponentName("com.alarm.critical.absent", "com.alarm.critical.absent.Reglage")
    }

    private lateinit var context: Context
    private lateinit var fakeApi: FakeApiService
    private lateinit var targets: List<Intent>
    private val scenarios = mutableListOf<ActivityScenario<*>>()

    private fun prefs() = context.getSharedPreferences("alarm_prefs", Context.MODE_PRIVATE)
    private fun installPrefs() = context.getSharedPreferences("install_prefs", Context.MODE_PRIVATE)
    private fun logs() = AppLogger.exportLogs("user1", "test")

    private fun waitFor(timeoutMs: Long = 10_000, cond: () -> Boolean): Boolean {
        val end = SystemClock.uptimeMillis() + timeoutMs
        while (SystemClock.uptimeMillis() < end) {
            if (cond()) return true
            SystemClock.sleep(100)
        }
        return cond()
    }

    private fun shell(cmd: String) = ParcelFileDescriptor.AutoCloseInputStream(
        InstrumentationRegistry.getInstrumentation().uiAutomation.executeShellCommand(cmd)).use { String(it.readBytes()) }

    /** Toast [TOAST] affiche pendant [action] (evenement d'accessibilite : depuis Android 11, le systeme dessine le toast). */
    private fun toastDuring(action: () -> Unit): Boolean = try {
        InstrumentationRegistry.getInstrumentation().uiAutomation.executeAndWaitForEvent({ action() }, { e ->
            e.eventType == AccessibilityEvent.TYPE_NOTIFICATION_STATE_CHANGED && e.text.joinToString("").contains(TOAST)
        }, 10_000)
        true
    } catch (e: TimeoutException) {
        false
    }

    private val alarm = AlarmResponse(
        id = 78, title = "Declenchement contact sec - Izeron", message = "", severity = "critical",
        status = "active", assigned_user_id = 1, acknowledged_at = null, acknowledged_by_name = null,
        suspended_until = null, escalation_count = 0, created_at = "2026-11-10T08:00:00", site = "izeron"
    )

    @Before
    fun setUp() {
        context = InstrumentationRegistry.getInstrumentation().targetContext
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            InstrumentationRegistry.getInstrumentation().uiAutomation
                .grantRuntimePermission(context.packageName, "android.permission.POST_NOTIFICATIONS")
        }
        fakeApi = FakeApiService()
        fakeApi.healthResponse = Response.success(HealthResponse("1.8.0"))  // a jour : pas de bandeau de mise a jour
        ApiProvider.override(fakeApi)
        UpdateProvider.override(SelfUpdateTest.FakeUpdateBackend(), "1.8.0")  // build release simule
        // Hors astreinte : pas de service de polling ; les deux autres demandes « une fois » deja faites
        prefs().edit().clear()
            .putString("token", "fake-token")
            .putString("user_name", "user1")
            .putBoolean("is_oncall", false)
            .putBoolean("battery_optimization_asked", true)
            .putBoolean("fsi_permission_asked", true)
            .commit()
        installPrefs().edit().clear().commit()  // installation neuve : Play Protect jamais demande
        AlarmPollingService.alarms = emptyList()
        AppLogger.clear()
        targets = PlayProtect.targets
        Intents.init()
        val ok = Instrumentation.ActivityResult(Activity.RESULT_OK, null)
        intending(hasComponent(PLAY_PROTECT)).respondWith(ok)
        intending(hasAction(Settings.ACTION_SECURITY_SETTINGS)).respondWith(ok)
    }

    @After
    fun tearDown() {
        Intents.release()
        scenarios.reversed().forEach { it.close() }
        PlayProtect.targets = targets
        AlarmSoundManager(context).stopAlarmSound()
        AlarmPollingService.alarms = emptyList()
        ApiProvider.reset()
        UpdateProvider.reset()
        prefs().edit().clear().commit()
    }

    private fun launchDashboard(): ActivityScenario<DashboardActivity> =
        ActivityScenario.launch<DashboardActivity>(
            Intent(context, DashboardActivity::class.java).putExtra("token", "fake-token")
        ).also { scenarios += it }

    /** App mise en arriere-plan puis rouverte : nouveau retour au premier plan. */
    private fun ActivityScenario<DashboardActivity>.backToForeground() {
        moveToState(Lifecycle.State.CREATED)
        moveToState(Lifecycle.State.RESUMED)
    }

    @Test
    fun premiere_ouverture_ouvre_le_reglage_play_protect_avec_le_toast() {
        assertTrue("toast « $TOAST » attendu", toastDuring { launchDashboard() })

        intended(hasComponent(PLAY_PROTECT), times(1))
        assertTrue("ouverture tracee dans les logs", logs().contains("Play Protect"))
    }

    @Test
    fun ouvertures_suivantes_plus_rien() {
        val first = launchDashboard()
        intended(hasComponent(PLAY_PROTECT), times(1))

        first.backToForeground()
        scenarios.remove(first)
        first.close()
        launchDashboard()  // app relancee
        intended(hasComponent(PLAY_PROTECT), times(1))
        intended(hasAction(Settings.ACTION_SECURITY_SETTINGS), times(0))
    }

    @Test
    fun alarme_en_cours_rien_puis_au_premier_retour_sans_alarme() {
        AlarmPollingService.alarms = listOf(alarm)
        val dashboard = launchDashboard()
        intended(hasComponent(PLAY_PROTECT), times(0))

        // Acquittee, l'alarme est toujours en cours : toujours rien
        AlarmPollingService.alarms = listOf(alarm.copy(status = "acknowledged", acknowledged_at = "2026-11-10T08:01:00",
            acknowledged_by_name = "user2", ack_remaining_seconds = 1800))
        dashboard.backToForeground()
        intended(hasComponent(PLAY_PROTECT), times(0))

        AlarmPollingService.alarms = emptyList()
        // Fin de l'alarme : le tableau de bord coupe la sonnerie a la seconde suivante
        assertTrue("plus de sonnerie", waitFor { !AlarmSoundManager(context).isPlaying() })
        dashboard.backToForeground()
        intended(hasComponent(PLAY_PROTECT), times(1))
    }

    @Test
    fun ecran_play_protect_introuvable_repli_sur_les_reglages_securite_puis_rien_sans_plantage() {
        PlayProtect.targets = listOf(Intent().setComponent(ABSENT)) + targets.drop(1)
        launchDashboard()
        intended(hasComponent(ABSENT), times(1))  // essaye d'abord
        intended(hasAction(Settings.ACTION_SECURITY_SETTINGS), times(1))

        // Ni l'un ni l'autre : rien n'est ouvert, l'app continue
        scenarios.removeAt(0).close()
        installPrefs().edit().clear().commit()
        PlayProtect.targets = listOf(Intent().setComponent(ABSENT), Intent("com.alarm.critical.test.AUCUN_REGLAGE"))
        val dashboard = launchDashboard()

        assertEquals("tableau de bord toujours ouvert", Lifecycle.State.RESUMED, dashboard.state)
        intended(hasAction(Settings.ACTION_SECURITY_SETTINGS), times(1))  // pas de nouvelle ouverture
        intended(hasComponent(PLAY_PROTECT), times(0))
        assertTrue("trace dans les logs", logs().contains("aucun réglage Play Protect"))
    }

    @Test
    fun bouton_reglage_play_protect_de_l_ecran_mise_a_jour() {
        UpdateProvider.override(SelfUpdateTest.FakeUpdateBackend())  // tout build, ici debug
        installPrefs().edit().putBoolean("play_protect_asked", true).commit()  // demande automatique deja faite
        scenarios += ActivityScenario.launch(UpdateActivity::class.java)
        intended(hasComponent(PLAY_PROTECT), times(0))

        assertTrue("toast « $TOAST » attendu", toastDuring {
            onView(withId(R.id.updatePlayProtectButton)).check(matches(withText("Réglage Play Protect")))
                .perform(scrollTo(), click())
        })
        intended(hasComponent(PLAY_PROTECT), times(1))
    }

    @Test
    fun build_debug_rien() {
        UpdateProvider.override(SelfUpdateTest.FakeUpdateBackend())  // version reelle du build : debug
        val dashboard = launchDashboard()
        dashboard.backToForeground()

        intended(hasComponent(PLAY_PROTECT), times(0))
        intended(hasAction(Settings.ACTION_SECURITY_SETTINGS), times(0))
    }

    @Test
    fun une_seule_demande_par_retour_au_premier_plan_batterie_puis_play_protect() {
        shell("dumpsys deviceidle whitelist -${context.packageName}")  // exemption batterie a demander
        assertFalse("pre-requis : app soumise a l'optimisation de la batterie",
            context.getSystemService(PowerManager::class.java).isIgnoringBatteryOptimizations(context.packageName))
        prefs().edit().putBoolean("battery_optimization_asked", false).commit()

        // Vraie boite de dialogue d'Android (pas de stub : son resultat immediat relancerait le tableau de bord)
        val dashboard = launchDashboard()
        assertTrue("boite « batterie » ouverte : tableau de bord en pause",
            waitFor { dashboard.state == Lifecycle.State.STARTED })
        intended(hasAction(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS), times(1))
        intended(hasComponent(PLAY_PROTECT), times(0))  // jamais deux ecrans de reglages a la fois

        UiDevice.getInstance(InstrumentationRegistry.getInstrumentation()).pressBack()  // boite fermee
        assertTrue("retour au tableau de bord", waitFor { dashboard.state == Lifecycle.State.RESUMED })
        intended(hasComponent(PLAY_PROTECT), times(1))
        intended(hasAction(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS), times(1))
    }
}
