package com.alarm.critical

import android.content.Context
import android.content.Intent
import android.graphics.drawable.GradientDrawable
import android.os.Build
import android.os.SystemClock
import android.view.View
import androidx.test.core.app.ActivityScenario
import androidx.test.espresso.Espresso.onView
import androidx.test.espresso.action.ViewActions.scrollTo
import androidx.test.espresso.assertion.PositionAssertions.isCompletelyAbove
import androidx.test.espresso.assertion.ViewAssertions.doesNotExist
import androidx.test.espresso.assertion.ViewAssertions.matches
import androidx.test.espresso.matcher.ViewMatchers.hasDescendant
import androidx.test.espresso.matcher.ViewMatchers.isDisplayed
import androidx.test.espresso.matcher.ViewMatchers.withContentDescription
import androidx.test.espresso.matcher.ViewMatchers.withId
import androidx.test.espresso.matcher.ViewMatchers.withParent
import androidx.test.espresso.matcher.ViewMatchers.withText
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.alarm.critical.api.ApiProvider
import com.alarm.critical.model.ActiveEscalationResponse
import com.alarm.critical.model.EscalationEntry
import com.alarm.critical.model.EscalationStep
import com.alarm.critical.service.AlarmPollingService
import okhttp3.ResponseBody.Companion.toResponseBody
import org.hamcrest.Matcher
import org.hamcrest.Matchers.allOf
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import retrofit2.Response

/**
 * INV-ANDROID-114 : la personne de garde (position 1 de la chaine) est reperee d'un coup d'oeil.
 * Ecran « Equipe » : sa ligne en tete, badge « DE GARDE », carte mise en evidence ; les autres par nom.
 * Tableau de bord : « DE GARDE » + son nom, pour tous. Pas de personne de garde : rien, sans erreur.
 */
@RunWith(AndroidJUnit4::class)
class OncallHighlightTest {

    private lateinit var context: Context
    private lateinit var fakeApi: FakeApiService
    private val scenarios = mutableListOf<ActivityScenario<*>>()

    private fun prefs() = context.getSharedPreferences("alarm_prefs", Context.MODE_PRIVATE)

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
        context.stopService(Intent(context, AlarmPollingService::class.java))
        val end = SystemClock.uptimeMillis() + 15000
        while (AlarmPollingService.isRunning && SystemClock.uptimeMillis() < end) SystemClock.sleep(100)
        ApiProvider.reset()
        prefs().edit().clear().commit()
    }

    private fun openTeam() { scenarios += ActivityScenario.launch(TeamActivity::class.java) }

    private fun openDashboard() {
        scenarios += ActivityScenario.launch<DashboardActivity>(
            Intent(context, DashboardActivity::class.java).putExtra("token", "fake-token")
        )
    }

    /** La carte d'une ligne de l'ecran Equipe (enfant direct du contenu). */
    private fun row(name: String): Matcher<View> = allOf(withParent(withId(R.id.screenContent)), hasDescendant(withText(name)))

    private fun above(upper: String, lower: String) = onView(withText(upper)).check(isCompletelyAbove(withText(lower)))

    /** Couleur de fond de la carte d'une ligne. */
    private fun background(name: String): Int? {
        var color: Int? = null
        onView(row(name)).perform(scrollTo()).check { v, e ->
            if (e != null) throw e
            color = (v.background as? GradientDrawable)?.color?.defaultColor
        }
        return color
    }

    private fun steps(vararg s: EscalationStep) = Response.success(ActiveEscalationResponse(null, 15.0, s.toList()))

    @Test
    fun equipe_personne_de_garde_en_tete_badge_et_mise_en_evidence() {
        // user2 de garde ; par nom : admin, user1, user2
        fakeApi.escalationChainResponse = Response.success(listOf(
            EscalationEntry(1, 2), EscalationEntry(2, 1), EscalationEntry(3, 3)
        ))
        openTeam()

        onView(withText("DE GARDE")).check(matches(isDisplayed()))  // un seul badge (sinon ambigu)
        onView(withText("DE GARDE")).check(matches(withParent(hasDescendant(withText("user2")))))
        onView(withContentDescription("De garde")).check(matches(isDisplayed()))  // TalkBack
        above("user2", "admin")
        above("admin", "user1")  // les autres gardent l'ordre par nom
        assertNotEquals("carte de la personne de garde mise en evidence", background("admin"), background("user2"))
        assertEquals("les autres cartes ne changent pas", background("admin"), background("user1"))
    }

    @Test
    fun equipe_sans_personne_de_garde_rien_en_evidence() {
        listOf<Response<List<EscalationEntry>>>(
            Response.success(emptyList()),
            Response.error(500, """{"detail":"boom"}""".toResponseBody()),
        ).forEach { chain ->
            fakeApi.escalationChainResponse = chain
            openTeam()

            onView(withText("user2")).perform(scrollTo()).check(matches(isDisplayed()))
            onView(withText("DE GARDE")).check(doesNotExist())
            above("admin", "user1")
            above("user1", "user2")
            assertEquals(background("admin"), background("user2"))
            scenarios.removeAt(scenarios.lastIndex).close()
        }
    }

    @Test
    fun tableau_de_bord_affiche_qui_est_de_garde() {
        // connecte en user1, de garde : user2
        fakeApi.activeEscalationResponse = steps(
            EscalationStep(1, 2, "user2", "upcoming", false),
            EscalationStep(2, 1, "user1", "upcoming", false),
        )
        openDashboard()

        onView(withContentDescription("De garde : user2")).check(matches(isDisplayed()))
        onView(allOf(withText("DE GARDE"), isDisplayed())).check(matches(isDisplayed()))
        onView(allOf(withText("user2"), isDisplayed())).check(matches(isDisplayed()))
    }

    @Test
    fun tableau_de_bord_sans_chaine_n_affiche_personne() {
        fakeApi.activeEscalationResponse = steps()
        openDashboard()

        onView(withId(R.id.userNameText)).check(matches(withText("user1")))  // dashboard charge
        onView(allOf(withText("DE GARDE"), isDisplayed())).check(doesNotExist())
    }
}
