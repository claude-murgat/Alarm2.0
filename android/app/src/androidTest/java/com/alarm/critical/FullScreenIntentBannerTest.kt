package com.alarm.critical

import android.app.Activity
import android.app.Instrumentation
import android.app.NotificationManager
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.ParcelFileDescriptor
import android.provider.Settings
import androidx.lifecycle.Lifecycle
import androidx.test.core.app.ActivityScenario
import androidx.test.espresso.Espresso.onView
import androidx.test.espresso.action.ViewActions.click
import androidx.test.espresso.assertion.ViewAssertions.matches
import androidx.test.espresso.intent.Intents
import androidx.test.espresso.intent.Intents.intended
import androidx.test.espresso.intent.Intents.intending
import androidx.test.espresso.intent.VerificationModes.times
import androidx.test.espresso.intent.matcher.IntentMatchers.hasAction
import androidx.test.espresso.intent.matcher.IntentMatchers.hasData
import androidx.test.espresso.matcher.ViewMatchers.isDisplayed
import androidx.test.espresso.matcher.ViewMatchers.withId
import androidx.test.espresso.matcher.ViewMatchers.withText
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.alarm.critical.api.ApiProvider
import com.alarm.critical.service.AlarmPollingService
import com.alarm.critical.util.AppLogger
import org.hamcrest.Matchers.allOf
import org.hamcrest.Matchers.not
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

/**
 * INV-ANDROID-709 : Android 14+, autorisation « plein écran » refusee → bandeau rouge permanent « Alarme plein écran
 * désactivée » sur le tableau de bord, verifie a chaque retour au premier plan ; son bouton ouvre le reglage Android de
 * l'app. L'autorisation est vraiment retiree et rendue (appops), comme sur le telephone ; le reglage est stubbe.
 */
@RunWith(AndroidJUnit4::class)
class FullScreenIntentBannerTest {

    private companion object {
        const val TITRE = "Alarme plein écran désactivée"
        const val BOUTON = "Autoriser le plein écran"
        const val LOG_AFFICHE = "bandeau « $TITRE » affiché"
        const val LOG_RETIRE = "autorisation plein écran accordée : bandeau retiré"
    }

    private lateinit var context: Context
    private val scenarios = mutableListOf<ActivityScenario<*>>()

    private fun prefs() = context.getSharedPreferences("alarm_prefs", Context.MODE_PRIVATE)
    private fun logs() = AppLogger.exportLogs("user1", "test")
    private fun count(text: String) = Regex.fromLiteral(text).findAll(logs()).count()

    private fun shell(cmd: String) = ParcelFileDescriptor.AutoCloseInputStream(
        InstrumentationRegistry.getInstrumentation().uiAutomation.executeShellCommand(cmd)).use { String(it.readBytes()) }

    /** Autorisation « plein écran » accordee ou retiree pour de vrai, comme dans les reglages du telephone */
    private fun fullScreen(mode: String) {
        shell("appops set ${context.packageName} USE_FULL_SCREEN_INTENT $mode")
        val expected = mode != "deny"
        assertEquals("pre-requis : autorisation plein écran $mode", expected,
            context.getSystemService(NotificationManager::class.java).canUseFullScreenIntent())
    }

    private val reglage = allOf(
        hasAction(Settings.ACTION_MANAGE_APP_USE_FULL_SCREEN_INTENT),
        hasData(Uri.parse("package:com.alarm.critical")),
    )

    @Before
    fun setUp() {
        context = InstrumentationRegistry.getInstrumentation().targetContext
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            InstrumentationRegistry.getInstrumentation().uiAutomation
                .grantRuntimePermission(context.packageName, "android.permission.POST_NOTIFICATIONS")
        }
        ApiProvider.override(FakeApiService())
        // Hors astreinte : pas de service ; les demandes « une fois » deja faites (pas l'objet de ce test)
        prefs().edit().clear()
            .putString("token", "fake-token")
            .putString("user_name", "user1")
            .putBoolean("is_oncall", false)
            .putBoolean("battery_optimization_asked", true)
            .putBoolean("fsi_permission_asked", true)
            .commit()
        AlarmPollingService.alarms = emptyList()
        AppLogger.clear()
        Intents.init()
        intending(hasAction(Settings.ACTION_MANAGE_APP_USE_FULL_SCREEN_INTENT))
            .respondWith(Instrumentation.ActivityResult(Activity.RESULT_OK, null))
    }

    @After
    fun tearDown() {
        Intents.release()
        scenarios.reversed().forEach { it.close() }
        if (Build.VERSION.SDK_INT >= 34) shell("appops set ${context.packageName} USE_FULL_SCREEN_INTENT default")
        ApiProvider.reset()
        prefs().edit().clear().commit()
    }

    private fun launchDashboard(): ActivityScenario<DashboardActivity> =
        ActivityScenario.launch<DashboardActivity>(
            Intent(context, DashboardActivity::class.java).putExtra("token", "fake-token")
        ).also { scenarios += it }

    /** App mise en arriere-plan puis rouverte (retour du reglage, par exemple) : nouveau retour au premier plan. */
    private fun ActivityScenario<DashboardActivity>.backToForeground() {
        moveToState(Lifecycle.State.CREATED)
        moveToState(Lifecycle.State.RESUMED)
    }

    private fun bandeauAffiche() {
        onView(withId(R.id.fsiBanner)).check(matches(isDisplayed()))
        onView(withId(R.id.fsiBannerTitle)).check(matches(allOf(isDisplayed(), withText(TITRE))))
        onView(withId(R.id.fsiBannerButton)).check(matches(allOf(isDisplayed(), withText(BOUTON))))
    }

    private fun pasDeBandeau() = onView(withId(R.id.fsiBanner)).check(matches(not(isDisplayed())))

    @Test
    fun autorisation_refusee_bandeau_rouge_et_bouton_vers_le_reglage_de_l_app() {
        assumeTrue("Android 14+ : l'autorisation plein écran se retire", Build.VERSION.SDK_INT >= 34)
        fullScreen("deny")
        launchDashboard()

        bandeauAffiche()
        intended(reglage, times(0))  // rien ne s'ouvre de lui-meme
        assertEquals("bandeau tracé dans les logs", 1, count(LOG_AFFICHE))

        onView(withId(R.id.fsiBannerButton)).perform(click())
        intended(reglage, times(1))
        assertTrue("appui tracé dans les logs", logs().contains("réglage « plein écran » ouvert"))
    }

    @Test
    fun verifiee_a_chaque_retour_au_premier_plan_pas_une_seule_fois() {
        assumeTrue("Android 14+ : l'autorisation plein écran se retire", Build.VERSION.SDK_INT >= 34)
        fullScreen("deny")
        val dashboard = launchDashboard()
        bandeauAffiche()

        // Accordee dans le reglage, retour dans l'app : plus de bandeau
        fullScreen("allow")
        dashboard.backToForeground()
        pasDeBandeau()
        assertEquals("retour de l'autorisation tracé", 1, count(LOG_RETIRE))

        // Retiree plus tard (utilisateur, systeme, mise a jour) : le bandeau revient
        fullScreen("deny")
        dashboard.backToForeground()
        bandeauAffiche()
        assertEquals("une ligne par apparition, pas par retour", 2, count(LOG_AFFICHE))

        // Toujours refusee : bandeau toujours la, sans nouvelle ligne
        dashboard.backToForeground()
        bandeauAffiche()
        assertEquals(2, count(LOG_AFFICHE))
        assertEquals(Lifecycle.State.RESUMED, dashboard.state)
    }

    @Test
    fun autorisation_accordee_jamais_de_bandeau() {
        if (Build.VERSION.SDK_INT >= 34) fullScreen("allow")  // Android 13 et avant : accordee a l'installation
        val dashboard = launchDashboard()
        pasDeBandeau()
        dashboard.backToForeground()
        pasDeBandeau()

        intended(reglage, times(0))
        assertFalse("aucun bandeau dans les logs", logs().contains(LOG_AFFICHE))
    }
}
