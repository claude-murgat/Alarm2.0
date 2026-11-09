package com.alarm.critical

import android.app.Activity
import android.app.Instrumentation
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.SystemClock
import android.provider.Settings
import androidx.lifecycle.Lifecycle
import androidx.test.core.app.ActivityScenario
import androidx.test.espresso.Espresso.onView
import androidx.test.espresso.action.ViewActions.click
import androidx.test.espresso.action.ViewActions.scrollTo
import androidx.test.espresso.assertion.ViewAssertions.doesNotExist
import androidx.test.espresso.assertion.ViewAssertions.matches
import androidx.test.espresso.intent.Intents
import androidx.test.espresso.intent.matcher.IntentMatchers.hasAction
import androidx.test.espresso.intent.matcher.IntentMatchers.hasData
import androidx.test.espresso.matcher.RootMatchers.isDialog
import androidx.test.espresso.matcher.ViewMatchers.isDisplayed
import androidx.test.espresso.matcher.ViewMatchers.withSubstring
import androidx.test.espresso.matcher.ViewMatchers.withText
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.alarm.critical.api.ApiProvider
import com.alarm.critical.model.AlarmResponse
import com.alarm.critical.model.HealthResponse
import com.alarm.critical.service.AlarmPollingService
import com.alarm.critical.service.AlarmSoundManager
import com.alarm.critical.update.AppUpdate
import com.alarm.critical.update.UpdateBackend
import com.alarm.critical.update.UpdateProvider
import com.alarm.critical.util.AppLogger
import org.hamcrest.Matchers.allOf
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import retrofit2.Response
import java.io.File
import java.security.MessageDigest
import java.util.concurrent.CopyOnWriteArrayList

/**
 * INV-ANDROID-705 : mise a jour integree. Le serveur annonce une release X.Y.Z plus recente
 * (/health.version) -> APK de la release GitHub telecharge, empreinte SHA-256 verifiee, bandeau
 * « Mettre a jour ». Jamais pendant une alarme, jamais en debug ni pour une version qui n'est pas X.Y.Z.
 * Telechargement et installeur remplaces par [FakeUpdateBackend] (UpdateProvider.override).
 */
@RunWith(AndroidJUnit4::class)
class SelfUpdateTest {

    /** Sert un faux APK et son empreinte [sha] (juste par defaut) ; note telechargements et installations. */
    class FakeUpdateBackend : UpdateBackend {
        val apk = "faux APK release 1.9.0".toByteArray()
        var sha = sha256(apk)
        var installAllowed = true
        val downloads = CopyOnWriteArrayList<String>()
        val installs = CopyOnWriteArrayList<File>()

        override fun download(url: String, dest: File, onProgress: (Int) -> Unit) {
            // Format de `sha256sum` (android-release.yml) : « <hex>  <nom> »
            dest.writeBytes(if (url.endsWith(".sha256")) "$sha  ${url.substringAfterLast('/').removeSuffix(".sha256")}\n".toByteArray() else apk)
            downloads += url
        }

        override fun canInstall(context: Context) = installAllowed

        override fun install(context: Context, apk: File) {
            installs += apk
        }
    }

    companion object {
        fun sha256(b: ByteArray) = MessageDigest.getInstance("SHA-256").digest(b).joinToString("") { "%02x".format(it) }
        const val APK_URL = "https://github.com/claude-murgat/Alarm2.0/releases/download/v1.9.0/alarme-murgat-v1.9.0-1.9.0.apk"
        const val BANNER = "Mise à jour disponible : 1.9.0"
    }

    private lateinit var context: Context
    private lateinit var fakeApi: FakeApiService
    private lateinit var fakeUpdate: FakeUpdateBackend
    private var scenario: ActivityScenario<DashboardActivity>? = null

    private fun prefs() = context.getSharedPreferences("alarm_prefs", Context.MODE_PRIVATE)

    private fun waitFor(timeoutMs: Long = 10_000, cond: () -> Boolean): Boolean {
        val end = SystemClock.uptimeMillis() + timeoutMs
        while (SystemClock.uptimeMillis() < end) {
            if (cond()) return true
            SystemClock.sleep(100)
        }
        return cond()
    }

    private fun shown(text: String) = waitFor { runCatching { onView(withText(text)).check(matches(isDisplayed())) }.isSuccess }

    private val alarm = AlarmResponse(
        id = 77, title = "Declenchement contact sec - Izeron", message = "", severity = "critical",
        status = "active", assigned_user_id = 1, acknowledged_at = null, acknowledged_by_name = null,
        suspended_until = null, escalation_count = 0, created_at = "2026-11-05T08:00:00", site = "izeron"
    )

    @Before
    fun setUp() {
        context = InstrumentationRegistry.getInstrumentation().targetContext
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            InstrumentationRegistry.getInstrumentation().uiAutomation
                .grantRuntimePermission(context.packageName, "android.permission.POST_NOTIFICATIONS")
        }
        fakeApi = FakeApiService()
        ApiProvider.override(fakeApi)
        fakeUpdate = FakeUpdateBackend()
        // Hors astreinte : pas de service de polling, l'alarme en cours est posee par le test
        prefs().edit().clear()
            .putString("token", "fake-token")
            .putString("user_name", "user1")
            .putBoolean("is_oncall", false)
            .putBoolean("battery_optimization_asked", true)
            .putBoolean("fsi_permission_asked", true)
            .commit()
        AlarmPollingService.alarms = emptyList()
        context.cacheDir.listFiles()?.forEach { it.deleteRecursively() }
        AppLogger.clear()
    }

    @After
    fun tearDown() {
        scenario?.close()
        AlarmSoundManager(context).stopAlarmSound()
        AlarmPollingService.alarms = emptyList()
        ApiProvider.reset()
        UpdateProvider.reset()
        prefs().edit().clear().commit()
    }

    /** Tableau de bord d'un telephone en version [installed] (null : version du build, debug ici). */
    private fun launch(server: String?, installed: String? = "1.8.0", debugBuild: Boolean = false) {
        fakeApi.healthResponse = Response.success(HealthResponse(server))
        if (debugBuild) UpdateProvider.override(fakeUpdate) else UpdateProvider.override(fakeUpdate, installed)
        scenario?.close()
        scenario = ActivityScenario.launch(Intent(context, DashboardActivity::class.java).putExtra("token", "fake-token"))
    }

    /** Rien n'est propose : ni telechargement, ni bandeau, ni installation. */
    private fun nothingOffered(case: String) {
        assertFalse("$case : rien ne doit etre telecharge", waitFor(3000) { fakeUpdate.downloads.isNotEmpty() })
        onView(withSubstring("Mise à jour")).check(doesNotExist())
        assertTrue("$case : rien ne doit etre installe", fakeUpdate.installs.isEmpty())
    }

    @Test
    fun serveur_plus_recent_bandeau_puis_installation_de_l_apk_verifie() {
        launch(server = "1.9.0")

        assertTrue("bandeau « $BANNER » attendu", shown(BANNER))
        assertEquals("APK et empreinte de la release GitHub", setOf("$APK_URL.sha256", APK_URL), fakeUpdate.downloads.toSet())
        assertTrue("rien n'est installe avant le bouton", fakeUpdate.installs.isEmpty())

        onView(withText("Mettre à jour")).perform(scrollTo(), click())

        assertTrue("installation lancee", waitFor { fakeUpdate.installs.size == 1 })
        assertArrayEquals("l'APK verifie est installe", fakeUpdate.apk, fakeUpdate.installs.single().readBytes())
    }

    @Test
    fun serveur_mis_a_jour_pendant_que_l_app_est_ouverte_bandeau_au_retour_au_premier_plan() {
        // 2026-11-06 : app ouverte avant le deploiement des serveurs -> rien pendant 6 h, sauf arret force
        launch(server = "1.8.0")
        nothingOffered("a l'ouverture, serveur a la meme version")

        fakeApi.healthResponse = Response.success(HealthResponse("1.9.0"))  // serveurs deployes ensuite
        scenario!!.moveToState(Lifecycle.State.CREATED)  // app en arriere-plan
        scenario!!.moveToState(Lifecycle.State.RESUMED)  // rouverte depuis les applis recentes

        assertTrue("bandeau « $BANNER » attendu au retour au premier plan", shown(BANNER))
        assertEquals("verification de fond toutes les 15 min (reprise apres un 404 avant publication de la release)",
            15 * 60_000L, AppUpdate.CHECK_INTERVAL_MS)
    }

    @Test
    fun meme_version_ou_plus_ancienne_pas_de_bandeau() {
        // 1.10.0 > 1.9.0 : comparaison numerique, pas alphabetique
        for ((installed, server) in listOf("1.8.0" to "1.8.0", "1.8.0" to "1.7.9", "1.10.0" to "1.9.0")) {
            val calls = fakeApi.healthCallCount
            launch(server = server, installed = installed)
            assertTrue("$installed / $server : /health interroge", waitFor { fakeApi.healthCallCount > calls })
            nothingOffered("$installed installee, serveur $server")
        }
    }

    @Test
    fun version_qui_n_est_pas_X_Y_Z_ignoree() {
        for ((installed, server) in listOf("1.8.0-3-gabc1234" to "1.9.0", "1.8.0" to "abc1234", "1.8.0" to "1.9.0-rc1", "1.8.0" to null)) {
            launch(server = server, installed = installed)
            nothingOffered("$installed installee, serveur $server")
        }
    }

    @Test
    fun build_debug_jamais_de_mise_a_jour() {
        launch(server = "9.9.9", debugBuild = true)  // version reelle du build : debug
        nothingOffered("build debug")
    }

    @Test
    fun empreinte_differente_rien_n_est_installe_et_l_apk_est_supprime() {
        fakeUpdate.sha = "0".repeat(64)
        launch(server = "1.9.0")

        assertTrue("APK telecharge", waitFor { fakeUpdate.downloads.contains(APK_URL) })
        assertTrue("echec trace dans les logs exportes",
            waitFor { AppLogger.exportLogs("user1", "test").contains("SHA-256 invalide") })
        assertTrue("APK supprime du cache", context.cacheDir.walk().none { it.name.endsWith(".apk") })
        onView(withSubstring("Mise à jour")).check(doesNotExist())
        assertTrue("rien n'est installe", fakeUpdate.installs.isEmpty())
    }

    @Test
    fun alarme_en_cours_installation_reportee_puis_lancee_a_la_fin_de_l_alarme() {
        AlarmPollingService.alarms = listOf(alarm)
        launch(server = "1.9.0")
        assertTrue(shown(BANNER))

        onView(withText("Mettre à jour")).perform(scrollTo(), click())

        assertTrue("report affiche", shown("Mise à jour 1.9.0 reportée : alarme en cours"))
        assertTrue("pas d'installation pendant l'alarme", fakeUpdate.installs.isEmpty())

        // Acquittee, l'alarme est toujours en cours : toujours rien
        AlarmPollingService.alarms = listOf(alarm.copy(status = "acknowledged", acknowledged_at = "2026-11-05T08:01:00",
            acknowledged_by_name = "user2", ack_remaining_seconds = 1800))
        assertFalse("pas d'installation pendant une alarme acquittee", waitFor(3000) { fakeUpdate.installs.isNotEmpty() })

        AlarmPollingService.alarms = emptyList()
        assertTrue("installation lancee a la fin de l'alarme", waitFor { fakeUpdate.installs.size == 1 })
        assertTrue(AppLogger.exportLogs("user1", "test").contains("reportee"))
    }

    @Test
    fun autorisation_absente_invite_vers_le_reglage_applis_inconnues() {
        fakeUpdate.installAllowed = false
        launch(server = "1.9.0")
        assertTrue(shown(BANNER))

        onView(withText("Mettre à jour")).perform(scrollTo(), click())

        onView(withText("Autorisation nécessaire")).inRoot(isDialog()).check(matches(isDisplayed()))
        Intents.init()
        try {
            Intents.intending(hasAction(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES))
                .respondWith(Instrumentation.ActivityResult(Activity.RESULT_OK, null))
            onView(withText("Ouvrir les réglages")).inRoot(isDialog()).perform(click())
            Intents.intended(allOf(hasAction(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES),
                hasData(Uri.parse("package:${context.packageName}"))))
        } finally {
            Intents.release()
        }
        assertTrue("rien n'est installe sans autorisation", fakeUpdate.installs.isEmpty())
    }
}
