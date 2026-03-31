package com.alarm.critical

import android.os.Bundle
import android.view.View
import android.view.WindowManager
import android.widget.Button
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import com.alarm.critical.api.ApiProvider
import com.alarm.critical.service.AlarmSoundManager
import kotlinx.coroutines.launch

class AlarmActivity : AppCompatActivity() {
    private lateinit var soundManager: AlarmSoundManager
    private var alarmId: Int = 0
    private var token: String? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // Show over lock screen
        window.addFlags(
            WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON or
            WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED or
            WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON
        )

        setContentView(R.layout.activity_alarm)

        alarmId = intent.getIntExtra("alarm_id", 0)
        val title = intent.getStringExtra("alarm_title") ?: "ALARME CRITIQUE"
        val message = intent.getStringExtra("alarm_message") ?: ""
        val severity = intent.getStringExtra("alarm_severity") ?: "critical"
        token = intent.getStringExtra("token")
            ?: getSharedPreferences("alarm_prefs", MODE_PRIVATE).getString("token", null)

        findViewById<TextView>(R.id.alarmTitle).text = title
        findViewById<TextView>(R.id.alarmMessage).text = message
        findViewById<TextView>(R.id.alarmSeverity).text = "GRAVITÉ : ${severity.uppercase()}"
        findViewById<TextView>(R.id.alarmId).text = "Alarme #$alarmId"

        // Start alarm sound
        soundManager = AlarmSoundManager(this)
        soundManager.startAlarmSound()

        // Acknowledge button
        findViewById<Button>(R.id.ackButton).setOnClickListener {
            acknowledgeAlarm()
        }
    }

    private fun acknowledgeAlarm() {
        if (token == null) {
            Toast.makeText(this, "Non authentifié", Toast.LENGTH_SHORT).show()
            return
        }

        lifecycleScope.launch {
            try {
                val response = ApiProvider.service.acknowledgeAlarm("Bearer $token", alarmId)
                if (response.isSuccessful) {
                    soundManager.stopAlarmSound()
                    runOnUiThread {
                        // Masquer le bouton acquitter
                        findViewById<Button>(R.id.ackButton).visibility = View.GONE

                        // Afficher le statut acquitté
                        val statusText = findViewById<TextView>(R.id.ackStatusText)
                        statusText.text = "\u2705 Acquittée"
                        statusText.visibility = View.VISIBLE

                        // Afficher le temps restant (30 min de suspension)
                        val remainingText = findViewById<TextView>(R.id.ackRemainingTime)
                        remainingText.text = "Suspendue pour 30 min restantes"
                        remainingText.visibility = View.VISIBLE
                    }
                } else {
                    runOnUiThread {
                        Toast.makeText(
                            this@AlarmActivity,
                            "Échec de l'acquittement : ${response.code()}",
                            Toast.LENGTH_SHORT
                        ).show()
                    }
                }
            } catch (e: Exception) {
                runOnUiThread {
                    Toast.makeText(
                        this@AlarmActivity,
                        "Erreur : ${e.message}",
                        Toast.LENGTH_SHORT
                    ).show()
                }
            }
        }
    }

    override fun onDestroy() {
        soundManager.stopAlarmSound()
        super.onDestroy()
    }

    override fun onBackPressed() {
        // Prevent dismissing alarm with back button
        // User must acknowledge
    }
}
