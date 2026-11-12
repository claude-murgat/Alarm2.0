package com.alarm.critical

import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.Canvas
import android.os.Build
import android.os.SystemClock
import android.widget.Button
import android.widget.ProgressBar
import android.widget.TextView
import androidx.core.view.GravityCompat
import androidx.drawerlayout.widget.DrawerLayout
import androidx.test.core.app.ActivityScenario
import androidx.test.espresso.Espresso.onView
import androidx.test.espresso.action.ViewActions.click
import androidx.test.espresso.action.ViewActions.scrollTo
import androidx.test.espresso.assertion.ViewAssertions.matches
import androidx.test.espresso.matcher.ViewMatchers.Visibility
import androidx.test.espresso.matcher.ViewMatchers.isEnabled
import androidx.test.espresso.matcher.ViewMatchers.withEffectiveVisibility
import androidx.test.espresso.matcher.ViewMatchers.withId
import androidx.test.espresso.matcher.ViewMatchers.withText
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.runner.lifecycle.ActivityLifecycleMonitorRegistry
import androidx.test.runner.lifecycle.Stage
import com.alarm.critical.api.ApiProvider
import com.alarm.critical.model.AlarmResponse
import com.alarm.critical.model.HealthResponse
import com.alarm.critical.service.AlarmPollingService
import com.alarm.critical.service.AlarmSoundManager
import com.alarm.critical.update.UpdateBackend
import com.alarm.critical.update.UpdateProvider
import com.alarm.critical.util.AppLogger
import org.hamcrest.Matchers.not
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
import java.io.IOException
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * INV-ANDROID-706 : ecran « Mise à jour » (versions installee et annoncee, etat en une phrase,
 * derniere verification, « Vérifier maintenant », « Installer ») et progression du telechargement
 * sur le bandeau. Un seul etat pour l'ecran et le bandeau d'INV-ANDROID-705.
 */
@RunWith(AndroidJUnit4::class)
class UpdateScreenTest {

    /** Faux telechargement : empreinte juste ; l'APK s'arrete a 42 % tant que [gate] est ferme, ou echoue ([apkFails]). */
    class GatedUpdateBackend : UpdateBackend {
        val apk = "faux APK release 1.9.0".toByteArray()
        @Volatile var gate: CountDownLatch? = null
        @Volatile var apkFails = false
        val downloads = CopyOnWriteArrayList<String>()
        val installs = CopyOnWriteArrayList<File>()

        override fun download(url: String, dest: File, onProgress: (Int) -> Unit) {
            downloads += url
            if (url.endsWith(".sha256")) {
                dest.writeText("${SelfUpdateTest.sha256(apk)}  ${url.substringAfterLast('/').removeSuffix(".sha256")}\n")
                return
            }
            if (apkFails) throw IOException("HTTP 404")
            onProgress(42)
            gate?.await(30, TimeUnit.SECONDS)
            dest.writeBytes(apk)
            onProgress(100)
        }

        override fun canInstall(context: Context) = true

        override fun install(context: Context, apk: File) {
            installs += apk
        }
    }

    companion object {
        const val READY = "Version 1.9.0 prête à installer"
    }

    private lateinit var context: Context
    private lateinit var fakeApi: FakeApiService
    private lateinit var fakeUpdate: GatedUpdateBackend
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

    /** Le texte de [id] devient [expected] (attente bornee). */
    private fun shows(id: Int, expected: String) =
        waitFor { runCatching { onView(withId(id)).check(matches(withText(expected))) }.isSuccess }

    private fun textOf(id: Int): String {
        var text = ""
        onView(withId(id)).check { v, e -> text = (v as? TextView)?.text?.toString() ?: throw e }
        return text
    }

    private fun progressOf(id: Int): Int {
        var p = -1
        onView(withId(id)).check { v, e -> p = (v as? ProgressBar)?.progress ?: throw e }
        return p
    }

    private fun visible(id: Int) = onView(withId(id)).check(matches(withEffectiveVisibility(Visibility.VISIBLE)))
    private fun gone(id: Int) = onView(withId(id)).check(matches(not(withEffectiveVisibility(Visibility.VISIBLE))))

    private val alarm = AlarmResponse(
        id = 77, title = "Declenchement contact sec - Izeron", message = "", severity = "critical",
        status = "active", assigned_user_id = 1, acknowledged_at = null, acknowledged_by_name = null,
        suspended_until = null, escalation_count = 0, created_at = "2026-11-09T08:00:00", site = "izeron"
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
        fakeUpdate = GatedUpdateBackend()
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
        fakeUpdate.gate?.countDown()
        scenarios.reversed().forEach { it.close() }
        // Ecran ouvert depuis le menu (hors scenario) : ferme aussi
        InstrumentationRegistry.getInstrumentation().runOnMainSync {
            val monitor = ActivityLifecycleMonitorRegistry.getInstance()
            listOf(Stage.RESUMED, Stage.PAUSED, Stage.STOPPED)
                .flatMap { monitor.getActivitiesInStage(it) }
                .forEach { it.finish() }
        }
        AlarmSoundManager(context).stopAlarmSound()
        AlarmPollingService.alarms = emptyList()
        ApiProvider.reset()
        UpdateProvider.reset()
        prefs().edit().clear().commit()
    }

    /** Telephone en version [installed] (null : build debug), serveur annoncant [server]. */
    private fun fakes(server: String?, installed: String?) {
        fakeApi.healthResponse = Response.success(HealthResponse(server))
        if (installed == null) UpdateProvider.override(fakeUpdate) else UpdateProvider.override(fakeUpdate, installed)
    }

    private fun launchDashboard(server: String?, installed: String? = "1.8.0"): ActivityScenario<DashboardActivity> {
        fakes(server, installed)
        return ActivityScenario.launch<DashboardActivity>(
            Intent(context, DashboardActivity::class.java).putExtra("token", "fake-token")
        ).also { scenarios += it }
    }

    private fun launchScreen(server: String?, installed: String? = "1.8.0") {
        fakes(server, installed)
        scenarios += ActivityScenario.launch(UpdateActivity::class.java)
    }

    /** Menu lateral ouvert sans animation, puis entree « Mise à jour » (au singulier, 2026-11-10). */
    private fun ActivityScenario<DashboardActivity>.openUpdateScreen() {
        onActivity { it.findViewById<DrawerLayout>(R.id.drawerLayout).openDrawer(GravityCompat.START, false) }
        onView(withId(R.id.drawerUpdate)).check(matches(withText("Mise à jour"))).perform(click())
    }

    /** Ce qu'on voit d'un bouton : couleur du fond (a gauche, hors du texte), couleur du texte, texte gras. */
    private fun lookOf(id: Int): List<Any> {
        var look = emptyList<Any>()
        onView(withId(id)).check { v, e ->
            val b = v as? Button ?: throw e
            val bmp = Bitmap.createBitmap(b.width, b.height, Bitmap.Config.ARGB_8888)
            b.draw(Canvas(bmp))
            look = listOf(Integer.toHexString(bmp.getPixel(b.width / 20, b.height / 2)),
                Integer.toHexString(b.currentTextColor), b.typeface?.isBold == true)
        }
        return look
    }

    @Test
    fun menu_ouvre_l_ecran_versions_installee_et_annoncee_a_jour() {
        launchDashboard(server = "1.8.0").openUpdateScreen()

        onView(withId(R.id.screenTitle)).check(matches(withText("Mise à jour")))
        assertTrue("version installee", shows(R.id.updateInstalled, "Version installée : 1.8.0"))
        assertTrue("version du serveur", shows(R.id.updateServer, "Dernière version annoncée par le serveur : 1.8.0"))
        assertTrue("etat", shows(R.id.updateState, "À jour"))
        val last = textOf(R.id.updateLastCheck)
        assertTrue("heure de la derniere verification : « $last »",
            Regex("""Dernière vérification : \d\d/\d\d \d\d:\d\d""").matches(last))
        gone(R.id.updateInstallButton)
        onView(withId(R.id.updateCheckButton)).check(matches(isEnabled()))
    }

    @Test
    fun progression_du_telechargement_sur_le_bandeau_et_l_ecran_puis_prete() {
        fakeUpdate.gate = CountDownLatch(1)  // APK retenu a 42 %
        val dashboard = launchDashboard(server = "1.9.0")

        assertTrue("bandeau pendant le telechargement",
            shows(R.id.updateBannerText, "Mise à jour 1.9.0 : téléchargement… 42\u00A0%"))
        visible(R.id.updateBannerProgress)
        assertEquals("barre du bandeau", 42, progressOf(R.id.updateBannerProgress))
        gone(R.id.updateButton)

        dashboard.openUpdateScreen()
        assertTrue("ecran pendant le telechargement",
            shows(R.id.updateState, "Version 1.9.0 disponible — téléchargement… 42\u00A0%"))
        visible(R.id.updateProgress)
        assertEquals("barre de l'ecran", 42, progressOf(R.id.updateProgress))
        onView(withId(R.id.updateCheckButton)).check(matches(not(isEnabled())))  // verification en cours
        gone(R.id.updateInstallButton)

        fakeUpdate.gate!!.countDown()

        assertTrue("APK verifie : pret", shows(R.id.updateState, READY))
        gone(R.id.updateProgress)
        visible(R.id.updateInstallButton)
        assertTrue("verification terminee : bouton actif",
            waitFor { runCatching { onView(withId(R.id.updateCheckButton)).check(matches(isEnabled())) }.isSuccess })

        onView(withId(R.id.backButton)).perform(click())  // retour au tableau de bord
        assertTrue("bandeau pret", shows(R.id.updateBannerText, "Mise à jour disponible : 1.9.0"))
        gone(R.id.updateBannerProgress)
        visible(R.id.updateButton)
    }

    @Test
    fun verifier_maintenant_relance_la_verification_sans_quitter_l_ecran() {
        launchScreen(server = "1.8.0")
        assertTrue(shows(R.id.updateState, "À jour"))
        val calls = fakeApi.healthCallCount

        fakeApi.healthResponse = Response.success(HealthResponse("1.9.0"))  // serveurs deployes pendant ce temps
        onView(withId(R.id.updateCheckButton)).perform(scrollTo(), click())

        assertTrue("version annoncee mise a jour",
            shows(R.id.updateServer, "Dernière version annoncée par le serveur : 1.9.0"))
        assertTrue("APK pret sans quitter l'ecran", shows(R.id.updateState, READY))
        assertTrue("/health interroge par le bouton", fakeApi.healthCallCount > calls)
    }

    @Test
    fun installer_depuis_l_ecran_installe_l_apk_verifie() {
        launchScreen(server = "1.9.0")
        assertTrue(shows(R.id.updateState, READY))

        onView(withId(R.id.updateInstallButton)).perform(scrollTo(), click())

        assertTrue("installation lancee", waitFor { fakeUpdate.installs.size == 1 })
        assertArrayEquals("l'APK verifie est installe", fakeUpdate.apk, fakeUpdate.installs.single().readBytes())
    }

    /** 2026-11-10, demande du proprietaire : « Installer » en bleu, comme « Mettre à jour » du bandeau. */
    @Test
    fun bouton_installer_du_meme_style_que_mettre_a_jour_du_bandeau() {
        launchDashboard(server = "1.9.0")
        assertTrue(shows(R.id.updateBannerText, "Mise à jour disponible : 1.9.0"))
        val banner = lookOf(R.id.updateButton)
        assertEquals("bouton du bandeau : fond bleu, texte blanc gras", listOf("ff00a2e5", "ffffffff", true), banner)

        scenarios += ActivityScenario.launch(UpdateActivity::class.java)
        assertTrue(shows(R.id.updateState, READY))
        visible(R.id.updateInstallButton)

        assertEquals("« Installer » du meme style que « Mettre à jour »", banner, lookOf(R.id.updateInstallButton))
    }

    @Test
    fun alarme_en_cours_installation_reportee_puis_lancee_a_la_fin_de_l_alarme() {
        AlarmPollingService.alarms = listOf(alarm)
        launchDashboard(server = "1.9.0").openUpdateScreen()
        assertTrue(shows(R.id.updateState, READY))

        onView(withId(R.id.updateInstallButton)).perform(scrollTo(), click())

        assertTrue("report affiche", shows(R.id.updateState, "Mise à jour reportée : alarme en cours"))
        assertFalse("pas d'installation pendant l'alarme", waitFor(3000) { fakeUpdate.installs.isNotEmpty() })

        AlarmPollingService.alarms = emptyList()
        assertTrue("installation lancee a la fin de l'alarme", waitFor { fakeUpdate.installs.size == 1 })
    }

    @Test
    fun telechargement_impossible_etat_d_echec() {
        fakeUpdate.apkFails = true
        launchScreen(server = "1.9.0")

        assertTrue(shows(R.id.updateState, "Téléchargement impossible, nouvel essai à la prochaine vérification"))
        gone(R.id.updateInstallButton)
        gone(R.id.updateProgress)
    }

    @Test
    fun serveur_injoignable_version_annoncee_inconnue_un_apk_pret_le_reste() {
        val unreachable = Response.error<HealthResponse>(503, okhttp3.ResponseBody.create(null, ""))
        UpdateProvider.override(fakeUpdate, "1.8.0")
        fakeApi.healthResponse = unreachable
        scenarios += ActivityScenario.launch(UpdateActivity::class.java)

        assertTrue(shows(R.id.updateServer, "Dernière version annoncée par le serveur : inconnue"))
        assertTrue(shows(R.id.updateState, "Serveur injoignable, nouvel essai à la prochaine vérification"))

        fakeApi.healthResponse = Response.success(HealthResponse("1.9.0"))
        onView(withId(R.id.updateCheckButton)).perform(scrollTo(), click())
        assertTrue(shows(R.id.updateState, READY))

        fakeApi.healthResponse = unreachable  // coupure passagere : l'APK verifie reste propose
        onView(withId(R.id.updateCheckButton)).perform(scrollTo(), click())
        assertTrue(shows(R.id.updateServer, "Dernière version annoncée par le serveur : inconnue"))
        assertTrue(shows(R.id.updateState, READY))
        visible(R.id.updateInstallButton)
    }

    @Test
    fun build_debug_message_de_developpement() {
        launchScreen(server = "9.9.9", installed = null)  // version reelle du build : debug

        assertTrue(shows(R.id.updateInstalled, "Version installée : ${BuildConfig.VERSION_NAME}"))
        assertTrue(shows(R.id.updateServer, "Dernière version annoncée par le serveur : 9.9.9"))
        assertTrue(shows(R.id.updateState, "Version de développement : pas de mise à jour"))
        gone(R.id.updateInstallButton)
        assertTrue("rien n'est telecharge", fakeUpdate.downloads.isEmpty())
    }
}
