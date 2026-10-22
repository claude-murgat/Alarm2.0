package com.alarm.critical

import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.SystemClock
import android.view.View
import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.alarm.critical.api.ApiProvider
import com.alarm.critical.service.AlarmPollingService
import org.junit.After
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

/**
 * INV-ANDROID-503 : la deconnexion retire le telephone cote serveur, meme si
 * l'ecran se ferme avant la reponse.
 *
 * Prod 2026-10-22 : timo, deconnecte de tous ses telephones, restait « En attente »
 * sur le web. L'appel partait dans lifecycleScope, annule par finish().
 */
@RunWith(AndroidJUnit4::class)
class LogoutDeviceTest {

    private lateinit var context: Context
    private lateinit var fakeApi: FakeApiService

    private fun prefs() = context.getSharedPreferences("alarm_prefs", Context.MODE_PRIVATE)

    private fun waitFor(timeoutMs: Long = 5000, cond: () -> Boolean): Boolean {
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
        fakeApi = FakeApiService().apply { deleteFcmDelayMs = 500 }
        ApiProvider.override(fakeApi)
        prefs().edit().clear()
            .putString("token", "fake-token")
            .putInt("user_id", 1)
            .putString("device_token", "telephone-timo")
            .putBoolean("battery_optimization_asked", true)
            .putBoolean("fsi_permission_asked", true)
            .commit()
    }

    @After
    fun tearDown() {
        context.stopService(Intent(context, AlarmPollingService::class.java))
        waitFor { !AlarmPollingService.isRunning }
        ApiProvider.reset()
        prefs().edit().clear().commit()
    }

    @Test
    fun deconnexion_retire_le_telephone_cote_serveur() {
        val scenario = ActivityScenario.launch<DashboardActivity>(
            Intent(context, DashboardActivity::class.java).putExtra("token", "fake-token")
        )
        // Le bouton vit dans le drawer ferme : meme idiome que AlarmE2ETest.test14
        scenario.onActivity { it.findViewById<View>(R.id.drawerLogout).performClick() }

        assertTrue("le serveur doit recevoir la suppression du telephone",
            waitFor { fakeApi.fcmTokenDeleted?.device_id == "telephone-timo" })
        scenario.close()
    }
}
