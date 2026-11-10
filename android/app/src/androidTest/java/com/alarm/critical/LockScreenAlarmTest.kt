package com.alarm.critical

import android.app.KeyguardManager
import android.app.NotificationManager
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.PowerManager
import android.os.SystemClock
import androidx.test.espresso.Espresso.onView
import androidx.test.espresso.assertion.ViewAssertions.matches
import androidx.test.espresso.matcher.ViewMatchers.withId
import androidx.test.espresso.matcher.ViewMatchers.withText
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.uiautomator.By
import androidx.test.uiautomator.UiDevice
import androidx.test.uiautomator.Until
import com.alarm.critical.api.ApiProvider
import com.alarm.critical.model.AlarmResponse
import com.alarm.critical.service.AlarmPollingService
import com.alarm.critical.service.AlarmSoundManager
import org.junit.After
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import retrofit2.Response

/**
 * INV-ANDROID-702 (verifie en cible Android 16, INV-ANDROID-708) : telephone verrouille par un code, ecran eteint,
 * app fermee. Une alarme recue par le vrai service ouvre l'ecran d'alarme par-dessus l'ecran de verrouillage et
 * il sonne. Aucune activite n'est lancee par le test : seule la notification plein ecran du service peut l'ouvrir.
 */
@RunWith(AndroidJUnit4::class)
class LockScreenAlarmTest {

    private companion object { const val PIN = "1234" }

    private lateinit var context: Context
    private lateinit var fakeApi: FakeApiService
    private val device get() = UiDevice.getInstance(InstrumentationRegistry.getInstrumentation())
    private val power get() = context.getSystemService(PowerManager::class.java)
    private val keyguard get() = context.getSystemService(KeyguardManager::class.java)

    private fun prefs() = context.getSharedPreferences("alarm_prefs", Context.MODE_PRIVATE)

    private fun waitFor(timeoutMs: Long = 10_000, cond: () -> Boolean): Boolean {
        val end = SystemClock.uptimeMillis() + timeoutMs
        while (SystemClock.uptimeMillis() < end) {
            if (cond()) return true
            SystemClock.sleep(100)
        }
        return cond()
    }

    private fun alarm(id: Int) = AlarmResponse(
        id = id, title = "Panne compresseur", message = "Atelier", severity = "critical", status = "active",
        assigned_user_id = 1, acknowledged_at = null, acknowledged_by_name = null, suspended_until = null,
        escalation_count = 0, created_at = "2026-01-01T00:00:00", site = "Izeron",
    )

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
        prefs().edit().clear().putString("token", "fake-token").putInt("user_id", 1).commit()
        AlarmPollingService.alarms = emptyList()
        device.executeShellCommand("locksettings clear --old $PIN")  // reste d'un run interrompu
        device.executeShellCommand("locksettings set-pin $PIN")
    }

    @After
    fun tearDown() {
        context.stopService(Intent(context, AlarmPollingService::class.java))
        waitFor { !AlarmPollingService.isRunning }
        AlarmActivity.closeIfShowing()
        AlarmSoundManager(context).stopAlarmSound()
        context.getSystemService(NotificationManager::class.java).cancel(AlarmPollingService.ALARM_NOTIFICATION_ID)
        AlarmPollingService.alarms = emptyList()
        ApiProvider.reset()
        prefs().edit().clear().commit()
        // Android 16 : animations a 0 (reglage des AVD de test), la sortie de l'ecran de verrouillage ne se termine jamais
        val scales = listOf("window_animation_scale", "transition_animation_scale", "animator_duration_scale")
        val saved = scales.map { device.executeShellCommand("settings get global $it").trim() }
        scales.forEach { device.executeShellCommand("settings put global $it 1") }
        device.wakeUp()
        // Saisir le code plutot que l'effacer d'abord : Android 11 resterait bloque sur le pave de code
        val unlocked = waitFor(20_000) {
            device.executeShellCommand("wm dismiss-keyguard")
            device.executeShellCommand("input text $PIN")
            device.pressEnter()
            !keyguard.isKeyguardLocked
        }
        device.executeShellCommand("locksettings clear --old $PIN")
        scales.zip(saved).forEach { (name, value) -> device.executeShellCommand("settings put global $name $value") }
        assertTrue("ecran deverrouille apres le test", unlocked)
    }

    @Test
    fun telephone_verrouille_l_alarme_du_service_s_affiche_par_dessus_et_sonne() {
        // App fermee, surveillance en route (meme demarrage que le tableau de bord), aucune alarme pour l'instant
        fakeApi.myAlarmsResponses = mutableListOf(Response.success(emptyList()))
        context.startForegroundService(
            Intent(context, AlarmPollingService::class.java).putExtra(AlarmPollingService.EXTRA_TOKEN, "fake-token")
        )
        assertTrue("service demarre", waitFor { AlarmPollingService.isRunning })

        device.sleep()  // bouton marche/arret
        assertTrue("ecran eteint et verrouille par un code",
            waitFor { !power.isInteractive && keyguard.isKeyguardLocked && keyguard.isDeviceSecure })

        fakeApi.myAlarmsResponses = mutableListOf(Response.success(listOf(alarm(77))))

        assertTrue("bouton « Acquitter » visible a l'ecran, sans deverrouiller",
            device.wait(Until.hasObject(By.res(context.packageName, "ackButton")), 30_000))
        assertTrue("ecran d'alarme au premier plan", AlarmActivity.isVisible)
        assertTrue("l'ecran s'est allume", power.isInteractive)
        assertTrue("toujours verrouille", keyguard.isKeyguardLocked)
        assertTrue("ca sonne", waitFor { AlarmSoundManager(context).isPlaying() })
        onView(withId(R.id.alarmId)).check(matches(withText("n°77")))
    }
}
