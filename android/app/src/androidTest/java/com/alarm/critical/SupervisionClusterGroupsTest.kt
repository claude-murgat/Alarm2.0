package com.alarm.critical

import android.content.Context
import androidx.test.core.app.ActivityScenario
import androidx.test.espresso.Espresso.onView
import androidx.test.espresso.action.ViewActions.scrollTo
import androidx.test.espresso.assertion.PositionAssertions.isCompletelyAbove
import androidx.test.espresso.assertion.ViewAssertions.doesNotExist
import androidx.test.espresso.assertion.ViewAssertions.matches
import androidx.test.espresso.matcher.ViewMatchers.isDisplayed
import androidx.test.espresso.matcher.ViewMatchers.withText
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.alarm.critical.api.ApiProvider
import com.alarm.critical.model.ClusterMember
import com.alarm.critical.model.ClusterResponse
import org.junit.After
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import retrofit2.Response

/**
 * INV-ANDROID-011 (decision du proprietaire 2026-11-05) : un seul cluster, affiche en groupes
 * Izeron, Beaufort, OVH puis « Legacy (Izeron) » en dernier. Groupe = `group` s'il est non vide,
 * sinon « Legacy (Izeron) » si `retiring`, sinon le lieu.
 */
@RunWith(AndroidJUnit4::class)
class SupervisionClusterGroupsTest {

    private lateinit var context: Context
    private lateinit var fakeApi: FakeApiService
    private var scenario: ActivityScenario<SupervisionActivity>? = null

    private fun prefs() = context.getSharedPreferences("alarm_prefs", Context.MODE_PRIVATE)

    @Before
    fun setUp() {
        context = InstrumentationRegistry.getInstrumentation().targetContext
        fakeApi = FakeApiService()
        ApiProvider.override(fakeApi)
        prefs().edit().clear().putString("token", "fake-token").commit()
    }

    @After
    fun tearDown() {
        scenario?.close()
        ApiProvider.reset()
        prefs().edit().clear().commit()
    }

    /** [cluster] null : les 7 noeuds par defaut de FakeApiService. */
    private fun open(cluster: ClusterResponse? = null) {
        cluster?.let { fakeApi.clusterResponse = Response.success(it) }
        scenario = ActivityScenario.launch(SupervisionActivity::class.java)
    }

    private fun shown(text: String) = onView(withText(text)).perform(scrollTo()).check(matches(isDisplayed()))
    private fun above(upper: String, lower: String) = onView(withText(upper)).check(isCompletelyAbove(withText(lower)))

    private fun cluster(vararg members: ClusterMember) =
        ClusterResponse(local_node = "node6", local_location = "izeron", local_machine = "alarm-3", members = members.toList())

    private val stats = "STATISTIQUES SUR 8 SEMAINES (HORS HEURES OUVRÉES)"  // section suivant le cluster

    /** Les 4 en-tetes, dans l'ordre ; alarm-3 sous Izeron, onsite-1 sous Legacy (Izeron) (dernier groupe). */
    private fun groupsInOrder() {
        listOf("Izeron", "Beaufort", "OVH", "Legacy (Izeron)").forEach { shown(it) }
        above("Izeron", "Beaufort")
        above("Beaufort", "OVH")
        above("OVH", "Legacy (Izeron)")
        above("Izeron", "alarm-3 · node6 · Izeron")
        above("alarm-3 · node6 · Izeron", "Beaufort")
        above("Legacy (Izeron)", "onsite-1 · node1 · Izeron · bientôt obsolète")
        above("onsite-1 · node1 · Izeron · bientôt obsolète", stats)
    }

    @Test
    fun groupes_calcules_sans_champ_group() {
        open()  // backend du 2026-11-05 : `retiring` et `location`, pas encore `group`

        groupsInOrder()
        above("Beaufort", "alarm-1 · node4 · Beaufort")
        above("alarm-1 · node4 · Beaufort", "OVH")
        above("OVH", "vps-1560caaa · node3 · OVH")
        above("vps-1560caaa · node3 · OVH", "Legacy (Izeron)")
    }

    @Test
    fun groupes_du_backend_champ_group() {
        // `retiring` absent : seul `group` peut placer onsite-1 sous « Legacy (Izeron) ».
        // alarm-4 : `group` vide -> calcule (son lieu, Izeron).
        open(cluster(
            ClusterMember("node1", "leader", "running", "izeron", "onsite-1", group = "Legacy (Izeron)"),
            ClusterMember("node2", "replica", "streaming", "izeron", "onsite-2", group = "Legacy (Izeron)"),
            ClusterMember("node3", "replica", "streaming", "ovh", "vps-1560caaa", group = "OVH"),
            ClusterMember("node4", "replica", "streaming", "beaufort", "alarm-1", group = "Beaufort"),
            ClusterMember("node5", "sync_standby", "streaming", "beaufort", "alarm-2", group = "Beaufort"),
            ClusterMember("node6", "replica", "streaming", "izeron", "alarm-3", group = "Izeron"),
            ClusterMember("node7", "replica", "streaming", "izeron", "alarm-4", group = ""),
        ))

        listOf("Izeron", "Beaufort", "OVH", "Legacy (Izeron)").forEach { shown(it) }
        above("Izeron", "Beaufort")
        above("Beaufort", "OVH")
        above("OVH", "Legacy (Izeron)")
        above("Izeron", "alarm-3 · node6 · Izeron")
        above("alarm-4 · node7 · Izeron", "Beaufort")
        above("Legacy (Izeron)", "onsite-1 · node1 · Izeron")
        above("onsite-1 · node1 · Izeron", stats)
    }

    @Test
    fun backend_d_avant_sans_group_ni_retiring_groupes_par_lieu() {
        open(cluster(
            ClusterMember("node1", "leader", "running", "izeron"),
            ClusterMember("node3", "replica", "streaming", "ovh"),
            ClusterMember("node4", "replica", "streaming", "beaufort"),
            ClusterMember("node6", "replica", "streaming", "izeron"),
        ))

        listOf("Izeron", "Beaufort", "OVH").forEach { shown(it) }
        above("Izeron", "Beaufort")
        above("Beaufort", "OVH")
        above("Izeron", "node1 · Izeron")
        above("node1 · Izeron", "Beaufort")
        above("node6 · Izeron", "Beaufort")
        onView(withText("Legacy (Izeron)")).check(doesNotExist())
    }
}
