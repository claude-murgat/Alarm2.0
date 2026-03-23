package com.alarm.critical

import android.os.Bundle
import android.view.WindowManager
import android.widget.Button
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import com.alarm.critical.api.ApiClient
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
        val title = intent.getStringExtra("alarm_title") ?: "CRITICAL ALARM"
        val message = intent.getStringExtra("alarm_message") ?: ""
        val severity = intent.getStringExtra("alarm_severity") ?: "critical"
        token = intent.getStringExtra("token")
            ?: getSharedPreferences("alarm_prefs", MODE_PRIVATE).getString("token", null)

        findViewById<TextView>(R.id.alarmTitle).text = title
        findViewById<TextView>(R.id.alarmMessage).text = message
        findViewById<TextView>(R.id.alarmSeverity).text = "SEVERITY: ${severity.uppercase()}"
        findViewById<TextView>(R.id.alarmId).text = "Alarm #$alarmId"

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
            Toast.makeText(this, "Not authenticated", Toast.LENGTH_SHORT).show()
            return
        }

        lifecycleScope.launch {
            try {
                val response = ApiClient.service.acknowledgeAlarm("Bearer $token", alarmId)
                if (response.isSuccessful) {
                    soundManager.stopAlarmSound()
                    runOnUiThread {
                        Toast.makeText(
                            this@AlarmActivity,
                            "Alarm acknowledged. Suspended for 30 minutes.",
                            Toast.LENGTH_LONG
                        ).show()
                        finish()
                    }
                } else {
                    runOnUiThread {
                        Toast.makeText(
                            this@AlarmActivity,
                            "Failed to acknowledge: ${response.code()}",
                            Toast.LENGTH_SHORT
                        ).show()
                    }
                }
            } catch (e: Exception) {
                runOnUiThread {
                    Toast.makeText(
                        this@AlarmActivity,
                        "Error: ${e.message}",
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
