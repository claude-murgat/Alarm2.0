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
import androidx.test.espresso.assertion.ViewAssertions.doesNotExist
import androidx.test.espresso.assertion.ViewAssertions.matches
import androidx.test.espresso.intent.Intents
import androidx.test.espresso.intent.Intents.intended
import androidx.test.espresso.intent.Intents.intending
import androidx.test.espresso.intent.VerificationModes.times
import androidx.test.espresso.intent.matcher.IntentMatchers.hasAction
import androidx.test.espresso.intent.matcher.IntentMatchers.hasComponent
import androidx.test.espresso.intent.matcher.IntentMatchers.hasPackage
import androidx.test.espresso.intent.matcher.IntentMatchers.toPackage
import androidx.test.espresso.matcher.RootMatchers.isDialog
import androidx.test.espresso.matcher.ViewMatchers.isDisplayed
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
 * INV-ANDROID-707 (amende le 2026-11-10) : boite « Réglage Play Protect » affichee d'elle-meme une fois par
 * installation (release, jamais pendant une alarme, une seule demande « une fois » par retour au premier plan), et par
 * le bouton de l'ecran « Mise à jour ». « Ouvrir le Play Store » lance le Play Store, sinon les reglages « Sécurité »,
 * sinon un toast. Jamais l'ecran interne de Google Play services (il charge sans fin sur un vrai telephone).
 * Ecrans stubbes par Espresso-Intents ; Play Store simule par PlayProtect.launcher (absent des AVD) ;
 * build release simule par UpdateProvider.override (INV-ANDROID-705).
 */
@RunWith(AndroidJUnit4::class)
class PlayProtectTest {

    companion object {
        /** Ecran interne de Google Play services : charge sans fin sur un vrai telephone, ne doit plus etre lance */
        val GMS_INTERNE = ComponentName("com.google.android.gms",
            "com.google.android.gms.security.settings.VerifyAppsSettingsActivity")
        const val GMS = "com.google.android.gms"
        const val PLAY_STORE = "com.android.vending"
        const val TITRE = "Réglage Play Protect"
        const val MESSAGE = "Une fois ce réglage activé, les mises à jour s'installent sans la fenêtre Play Protect.\n\n" +
            "1. « Ouvrir le Play Store » ;\n" +
            "2. touchez votre photo de profil (en haut à droite) ;\n" +
            "3. « Play Protect » ;\n" +
            "4. la roue ⚙ en haut à droite ;\n" +
            "5. activez « Améliorer la détection des applis malveillantes »."
        const val TOAST = "Activez « Améliorer la détection des applis malveillantes »"
        const val TOAST_RIEN = "Ouvrez le Play Store à la main"
        const val LOG_BOITE = "boîte « Réglage Play Protect » affichée"
    }

    private lateinit var context: Context
    private lateinit var fakeApi: FakeApiService
    private lateinit var launcher: (Context, String) -> Intent?
    private lateinit var fallback: Intent
    private val scenarios = mutableListOf<ActivityScenario<*>>()

    private fun prefs() = context.getSharedPreferences("alarm_prefs", Context.MODE_PRIVATE)
    private fun installPrefs() = context.getSharedPreferences("install_prefs", Context.MODE_PRIVATE)
    private fun logs() = AppLogger.exportLogs("user1", "test")
    private fun boitesAffichees() = Regex.fromLiteral(LOG_BOITE).findAll(logs()).count()

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

    /** Toast [text] affiche pendant [action] (evenement d'accessibilite : depuis Android 11, le systeme dessine le toast). */
    private fun toastDuring(text: String, action: () -> Unit): Boolean = try {
        InstrumentationRegistry.getInstrumentation().uiAutomation.executeAndWaitForEvent({ action() }, { e ->
            e.eventType == AccessibilityEvent.TYPE_NOTIFICATION_STATE_CHANGED && e.text.joinToString("") == text
        }, 10_000)
        true
    } catch (e: TimeoutException) {
        false
    }

    /** La boite de l'app, avec les etapes exactes et ses deux boutons. */
    private fun boiteAffichee() {
        onView(withText(TITRE)).inRoot(isDialog()).check(matches(isDisplayed()))
        onView(withId(android.R.id.message)).inRoot(isDialog()).check(matches(withText(MESSAGE)))
        onView(withText("Ouvrir le Play Store")).inRoot(isDialog()).check(matches(isDisplayed()))
        onView(withText("Plus tard")).inRoot(isDialog()).check(matches(isDisplayed()))
    }

    private fun ouvrirLePlayStore() = onView(withText("Ouvrir le Play Store")).inRoot(isDialog()).perform(click())

    /** Ni l'ecran interne de Google Play services, ni aucun autre ecran de Google Play services. */
    private fun jamaisGooglePlayServices() {
        intended(hasComponent(GMS_INTERNE), times(0))
        intended(toPackage(GMS), times(0))
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
        launcher = PlayProtect.launcher
        fallback = PlayProtect.fallback
        // Play Store installe (absent des AVD) : son lanceur, comme getLaunchIntentForPackage
        PlayProtect.launcher = { _, pkg -> Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER).setPackage(pkg) }
        Intents.init()
        val ok = Instrumentation.ActivityResult(Activity.RESULT_OK, null)
        intending(hasPackage(PLAY_STORE)).respondWith(ok)
        intending(hasAction(Settings.ACTION_SECURITY_SETTINGS)).respondWith(ok)
        intending(hasComponent(GMS_INTERNE)).respondWith(ok)  // jamais attendu ; stubbe pour ne pas l'ouvrir s'il partait
    }

    @After
    fun tearDown() {
        Intents.release()
        scenarios.reversed().forEach { it.close() }
        PlayProtect.launcher = launcher
        PlayProtect.fallback = fallback
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
    fun premiere_ouverture_boite_reglage_play_protect_jamais_l_ecran_interne_de_google_play_services() {
        launchDashboard()

        // Rien ne s'ouvre de lui-meme, surtout pas l'ecran interne de Google Play services
        jamaisGooglePlayServices()
        intended(hasPackage(PLAY_STORE), times(0))
        intended(hasAction(Settings.ACTION_SECURITY_SETTINGS), times(0))
        boiteAffichee()
        assertEquals("boîte tracée dans les logs", 1, boitesAffichees())
    }

    @Test
    fun ouvrir_le_play_store_lance_le_play_store_avec_le_toast() {
        val dashboard = launchDashboard()
        boiteAffichee()

        assertTrue("toast « $TOAST » attendu", toastDuring(TOAST) { ouvrirLePlayStore() })
        intended(hasPackage(PLAY_STORE), times(1))
        intended(hasAction(Settings.ACTION_SECURITY_SETTINGS), times(0))
        jamaisGooglePlayServices()
        assertTrue("Play Store tracé dans les logs", logs().contains("Play Store ouvert"))

        // Retour dans l'app : plus de boite
        dashboard.backToForeground()
        assertEquals("une seule boîte", 1, boitesAffichees())
        onView(withText(TITRE)).check(doesNotExist())
    }

    @Test
    fun plus_tard_puis_ouvertures_suivantes_plus_rien() {
        val first = launchDashboard()
        boiteAffichee()
        onView(withText("Plus tard")).inRoot(isDialog()).perform(click())
        onView(withText(TITRE)).check(doesNotExist())

        first.backToForeground()
        scenarios.remove(first)
        first.close()
        launchDashboard()  // app relancee

        assertEquals("une seule boîte, quelle que soit la réponse", 1, boitesAffichees())
        onView(withText(TITRE)).check(doesNotExist())
        intended(hasPackage(PLAY_STORE), times(0))
        intended(hasAction(Settings.ACTION_SECURITY_SETTINGS), times(0))
        jamaisGooglePlayServices()
    }

    @Test
    fun alarme_en_cours_rien_puis_au_premier_retour_sans_alarme() {
        AlarmPollingService.alarms = listOf(alarm)
        val dashboard = launchDashboard()
        assertEquals("pas de boîte pendant l'alarme", 0, boitesAffichees())

        // Acquittee, l'alarme est toujours en cours : toujours rien
        AlarmPollingService.alarms = listOf(alarm.copy(status = "acknowledged", acknowledged_at = "2026-11-10T08:01:00",
            acknowledged_by_name = "user2", ack_remaining_seconds = 1800))
        dashboard.backToForeground()
        assertEquals("pas de boîte pendant l'alarme acquittée", 0, boitesAffichees())
        assertTrue("report tracé dans les logs", logs().contains("repoussée : alarme en cours"))

        AlarmPollingService.alarms = emptyList()
        // Fin de l'alarme : le tableau de bord coupe la sonnerie a la seconde suivante
        assertTrue("plus de sonnerie", waitFor { !AlarmSoundManager(context).isPlaying() })
        dashboard.backToForeground()
        jamaisGooglePlayServices()
        boiteAffichee()
        assertEquals(1, boitesAffichees())
    }

    @Test
    fun play_store_absent_repli_sur_les_reglages_securite_puis_rien_sans_plantage() {
        PlayProtect.launcher = { _, _ -> null }  // pas de Play Store sur ce telephone
        launchDashboard()
        boiteAffichee()
        assertTrue("toast « $TOAST » attendu", toastDuring(TOAST) { ouvrirLePlayStore() })
        intended(hasAction(Settings.ACTION_SECURITY_SETTINGS), times(1))
        assertTrue("repli tracé dans les logs", logs().contains("Play Store absent"))

        // Ni Play Store ni reglages « Sécurité » : un toast, rien n'est ouvert, l'app continue
        scenarios.removeAt(0).close()
        installPrefs().edit().clear().commit()
        PlayProtect.fallback = Intent("com.alarm.critical.test.AUCUN_REGLAGE")
        val dashboard = launchDashboard()
        boiteAffichee()
        assertTrue("toast « $TOAST_RIEN » attendu", toastDuring(TOAST_RIEN) { ouvrirLePlayStore() })

        assertEquals("tableau de bord toujours ouvert", Lifecycle.State.RESUMED, dashboard.state)
        intended(hasAction(Settings.ACTION_SECURITY_SETTINGS), times(1))  // pas de nouvelle ouverture
        jamaisGooglePlayServices()
        assertTrue("tracé dans les logs", logs().contains("aucun réglage Play Protect"))
    }

    @Test
    fun bouton_reglage_play_protect_de_l_ecran_mise_a_jour_affiche_la_boite() {
        UpdateProvider.override(SelfUpdateTest.FakeUpdateBackend())  // tout build, ici debug
        installPrefs().edit().putBoolean("play_protect_asked", true).commit()  // demande automatique deja faite
        scenarios += ActivityScenario.launch(UpdateActivity::class.java)
        assertEquals("pas de boîte à l'ouverture de l'écran", 0, boitesAffichees())

        onView(withId(R.id.updatePlayProtectButton)).check(matches(withText(TITRE))).perform(scrollTo(), click())
        jamaisGooglePlayServices()
        intended(hasPackage(PLAY_STORE), times(0))  // rien ne s'ouvre avant la reponse
        boiteAffichee()

        assertTrue("toast « $TOAST » attendu", toastDuring(TOAST) { ouvrirLePlayStore() })
        intended(hasPackage(PLAY_STORE), times(1))
    }

    @Test
    fun build_debug_rien() {
        UpdateProvider.override(SelfUpdateTest.FakeUpdateBackend())  // version reelle du build : debug
        val dashboard = launchDashboard()
        dashboard.backToForeground()

        assertEquals("pas de boîte en debug", 0, boitesAffichees())
        onView(withText(TITRE)).check(doesNotExist())
        intended(hasPackage(PLAY_STORE), times(0))
        intended(hasAction(Settings.ACTION_SECURITY_SETTINGS), times(0))
        jamaisGooglePlayServices()
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
        assertEquals("jamais deux demandes à la fois", 0, boitesAffichees())

        UiDevice.getInstance(InstrumentationRegistry.getInstrumentation()).pressBack()  // boite fermee
        assertTrue("retour au tableau de bord", waitFor { dashboard.state == Lifecycle.State.RESUMED })
        jamaisGooglePlayServices()
        boiteAffichee()
        assertEquals(1, boitesAffichees())
        intended(hasAction(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS), times(1))
    }
}
