package com.alarm.critical

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.widget.Button
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.alarm.critical.service.AlarmPollingService
import kotlinx.coroutines.*

class DashboardActivity : AppCompatActivity() {
    private var token: String? = null
    private var statusUpdateJob: Job? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_dashboard)

        token = intent.getStringExtra("token")

        // Request notification permission on Android 13+
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED
            ) {
                ActivityCompat.requestPermissions(
                    this, arrayOf(Manifest.permission.POST_NOTIFICATIONS), 100
                )
            }
        }

        val prefs = getSharedPreferences("alarm_prefs", MODE_PRIVATE)
        val userName = prefs.getString("user_name", "User") ?: "User"
        val userEmail = prefs.getString("user_email", "") ?: ""

        findViewById<TextView>(R.id.userNameText).text = "Welcome, $userName"
        findViewById<TextView>(R.id.userEmailText).text = userEmail

        // Start polling service
        startPollingService()

        // Logout button
        findViewById<Button>(R.id.logoutButton).setOnClickListener {
            prefs.edit().clear().apply()
            stopPollingService()
            startActivity(Intent(this, MainActivity::class.java))
            finish()
        }

        // Update status every second
        statusUpdateJob = lifecycleScope.launch {
            while (isActive) {
                updateStatus()
                delay(1000)
            }
        }
    }

    private fun startPollingService() {
        val intent = Intent(this, AlarmPollingService::class.java)
        intent.putExtra(AlarmPollingService.EXTRA_TOKEN, token)
        ContextCompat.startForegroundService(this, intent)
    }

    private fun stopPollingService() {
        stopService(Intent(this, AlarmPollingService::class.java))
    }

    private fun updateStatus() {
        runOnUiThread {
            findViewById<TextView>(R.id.connectionStatus).text =
                "Connection: ${AlarmPollingService.lastConnectionStatus}"
            findViewById<TextView>(R.id.serviceStatus).text =
                "Service: ${if (AlarmPollingService.isRunning) "Running" else "Stopped"}"
            findViewById<TextView>(R.id.alarmStatus).text =
                "Active Alarms: ${AlarmPollingService.activeAlarmCount}"
        }
    }

    override fun onDestroy() {
        statusUpdateJob?.cancel()
        super.onDestroy()
    }
}
