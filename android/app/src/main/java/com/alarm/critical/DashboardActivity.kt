package com.alarm.critical

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.view.View
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import android.util.Log
import com.alarm.critical.api.ApiProvider
import com.alarm.critical.model.AlarmResponse
import com.alarm.critical.model.FcmTokenRequest
import com.alarm.critical.service.AlarmPollingService
import com.alarm.critical.service.AlarmSoundManager
import com.google.firebase.messaging.FirebaseMessaging
import kotlinx.coroutines.tasks.await
import kotlinx.coroutines.*
import java.text.SimpleDateFormat
import java.util.*

class DashboardActivity : AppCompatActivity() {
    private var token: String? = null
    private var statusUpdateJob: Job? = null
    private var historyJob: Job? = null
    private var soundManager: AlarmSoundManager? = null
    private var connectionLostSoundManager: AlarmSoundManager? = null
    private var currentAlarm: AlarmResponse? = null
    private var isAcknowledged = false
    private var alarmGoneDuringAck = false  // true quand l'alarme a disparu après ack (suspension)
    private var acknowledgedAlarmId: Int? = null  // ID de l'alarme acquittée par cet utilisateur

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_dashboard)

        token = intent.getStringExtra("token")

        // Permission notification Android 13+
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
        val userName = prefs.getString("user_name", "Utilisateur") ?: "Utilisateur"

        // Afficher uniquement le nom (pas de bienvenue, pas d'email)
        findViewById<TextView>(R.id.userNameText).text = userName

        // Enregistrer le token FCM au demarrage (couvre le cas SharedPrefs injectees)
        lifecycleScope.launch {
            try {
                val fcmToken = FirebaseMessaging.getInstance().token.await()
                val deviceId = prefs.getString("device_token", "unknown") ?: "unknown"
                ApiProvider.service.registerFcmToken(
                    "Bearer $token",
                    FcmTokenRequest(token = fcmToken, device_id = deviceId)
                )
                Log.d("Dashboard", "FCM token registered: ${fcmToken.take(20)}...")
            } catch (e: Exception) {
                Log.e("Dashboard", "FCM token registration failed: ${e.message}")
            }
        }

        // Demarrer le service de polling UNIQUEMENT si on-call ou reveille par FCM
        val isOncall = prefs.getBoolean("is_oncall", true)
        val startedByFcm = prefs.getBoolean("started_by_fcm", false)
        if (isOncall || startedByFcm) {
            startPollingService()
        }

        // Bouton déconnexion
        findViewById<Button>(R.id.logoutButton).setOnClickListener {
            prefs.edit().clear().apply()
            stopPollingService()
            soundManager?.stopAlarmSound()
            startActivity(Intent(this, MainActivity::class.java))
            finish()
        }

        // Bouton acquitter
        findViewById<Button>(R.id.dashboardAckButton).setOnClickListener {
            acknowledgeCurrentAlarm()
        }

        // Mise à jour du statut toutes les secondes
        statusUpdateJob = lifecycleScope.launch {
            while (isActive) {
                updateStatus()
                delay(1000)
            }
        }

        // Charger l'historique toutes les 10 secondes
        historyJob = lifecycleScope.launch {
            while (isActive) {
                loadHistory()
                delay(10000)
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
            // Statut connexion avec icône
            findViewById<TextView>(R.id.connectionStatus).text =
                if (AlarmPollingService.lastHeartbeatOk) "\u2705 Connexion avec le serveur ok"
                else "\u274C Déconnecté"

            // Alerte connexion perdue (après timeout heartbeat)
            val connectionLostAlert = findViewById<TextView>(R.id.connectionLostAlert)
            if (AlarmPollingService.authErrorAlarm) {
                // Session expirée — sonnerie continue + message permanent
                connectionLostAlert.text = "\u26A0\uFE0F ${AlarmPollingService.authErrorMessage}"
                connectionLostAlert.visibility = View.VISIBLE
                if (connectionLostSoundManager == null) {
                    connectionLostSoundManager = AlarmSoundManager(this@DashboardActivity)
                }
                connectionLostSoundManager?.startAlarmSound()
            } else if (AlarmPollingService.heartbeatLostAlarm) {
                connectionLostAlert.text = "\u26A0\uFE0F Connexion perdue avec le serveur"
                connectionLostAlert.visibility = View.VISIBLE
                if (connectionLostSoundManager == null) {
                    connectionLostSoundManager = AlarmSoundManager(this@DashboardActivity)
                }
                connectionLostSoundManager?.startAlarmSound()
            } else {
                connectionLostAlert.visibility = View.GONE
                connectionLostSoundManager?.stopAlarmSound()
            }

            // Statut service
            findViewById<TextView>(R.id.serviceStatus).text =
                "Service : ${if (AlarmPollingService.isRunning) "En cours" else "Arrêté"}"

            // Alarme en cours
            val alarm = AlarmPollingService.currentAlarm
            val alarmLine = findViewById<TextView>(R.id.currentAlarmLine)
            val titleView = findViewById<TextView>(R.id.alarmTitle)
            val messageView = findViewById<TextView>(R.id.alarmMessage)
            val durationView = findViewById<TextView>(R.id.alarmDuration)
            val ackButton = findViewById<Button>(R.id.dashboardAckButton)

            val ackStatus = findViewById<TextView>(R.id.ackStatusText)
            val ackRemaining = findViewById<TextView>(R.id.ackRemainingTime)

            val currentUserName = getSharedPreferences("alarm_prefs", MODE_PRIVATE)
                .getString("user_name", "") ?: ""

            // Reset ack si une AUTRE alarme arrive (ID différent)
            if (alarm != null && isAcknowledged && alarm.id != acknowledgedAlarmId) {
                isAcknowledged = false
                alarmGoneDuringAck = false
                acknowledgedAlarmId = null
            }

            // Si l'alarme revient après avoir disparu pendant l'ack → fin de suspension
            if (alarm != null && isAcknowledged && alarmGoneDuringAck) {
                isAcknowledged = false
                alarmGoneDuringAck = false
            }

            if (alarm != null && alarm.status == "acknowledged" && !isAcknowledged) {
                // Alarme acquittée par quelqu'un d'autre → afficher info, pas de son, pas de bouton
                currentAlarm = alarm
                val ackerName = alarm.acknowledged_by_name ?: "?"
                alarmLine.text = "\uD83D\uDD34 Alarme active"  // 🔴

                titleView.text = alarm.title
                titleView.visibility = View.VISIBLE
                messageView.text = alarm.message
                messageView.visibility = View.VISIBLE

                val duration = computeDuration(alarm.created_at)
                durationView.text = "Active depuis $duration"
                durationView.visibility = View.VISIBLE

                ackButton.visibility = View.GONE

                ackStatus.text = "\u2705 Acquitt\u00e9e par $ackerName"
                ackStatus.visibility = View.VISIBLE

                val remaining = alarm.ack_remaining_seconds ?: 0
                val min = remaining / 60
                ackRemaining.text = "$min min restantes"
                ackRemaining.visibility = View.VISIBLE

                soundManager?.stopAlarmSound()

            } else if (alarm != null && !isAcknowledged) {
                // Alarme active et non acquittée → afficher alarme + sonnerie
                currentAlarm = alarm

                alarmLine.text = "\uD83D\uDD34 Alarme active"  // 🔴

                titleView.text = alarm.title
                titleView.visibility = View.VISIBLE

                messageView.text = alarm.message
                messageView.visibility = View.VISIBLE

                val duration = computeDuration(alarm.created_at)
                durationView.text = "Active depuis $duration"
                durationView.visibility = View.VISIBLE

                ackButton.visibility = View.VISIBLE
                ackStatus.visibility = View.GONE
                ackRemaining.visibility = View.GONE

                if (soundManager == null) {
                    soundManager = AlarmSoundManager(this)
                }
                soundManager?.startAlarmSound()

            } else if (alarm != null && isAcknowledged) {
                // Alarme acquittée par nous → garder l'affichage, countdown dynamique
                currentAlarm = alarm
                alarmLine.text = "\uD83D\uDD34 Alarme active"  // 🔴

                titleView.text = alarm.title
                titleView.visibility = View.VISIBLE
                messageView.text = alarm.message
                messageView.visibility = View.VISIBLE

                val duration = computeDuration(alarm.created_at)
                durationView.text = "Active depuis $duration"
                durationView.visibility = View.VISIBLE

                ackButton.visibility = View.GONE

                // Mettre à jour le countdown depuis le serveur
                val remaining = alarm.ack_remaining_seconds ?: 0
                val min = remaining / 60
                ackRemaining.text = "$min min restantes"
                ackRemaining.visibility = View.VISIBLE

            } else if (alarm == null && isAcknowledged) {
                // Alarme disparue (résolue pendant ack) → garder l'affichage acquitté
                alarmGoneDuringAck = true
                alarmLine.text = "\u26AA Aucune alarme"  // ⚪
                titleView.visibility = View.GONE
                messageView.visibility = View.GONE
                durationView.visibility = View.GONE
                ackButton.visibility = View.GONE
                // ackStatus et ackRemaining restent visibles

                soundManager?.stopAlarmSound()

            } else {
                // Pas d'alarme, pas d'acquittement
                currentAlarm = null
                isAcknowledged = false
                alarmLine.text = "\u26AA Aucune alarme"  // ⚪

                titleView.visibility = View.GONE
                messageView.visibility = View.GONE
                durationView.visibility = View.GONE
                ackButton.visibility = View.GONE

                soundManager?.stopAlarmSound()

                ackStatus.visibility = View.GONE
                ackRemaining.visibility = View.GONE
            }

            // Indicateur sonnerie
            val isSounding = (soundManager?.isPlaying() == true) ||
                             (connectionLostSoundManager?.isPlaying() == true)
            findViewById<TextView>(R.id.soundStatus).text =
                if (isSounding) "\uD83D\uDD14 Sonnerie ACTIVE"   // 🔔
                else "\uD83D\uDD07 Sonnerie inactive"             // 🔇
            findViewById<TextView>(R.id.soundStatus).setTextColor(
                if (isSounding) android.graphics.Color.parseColor("#ef4444")
                else android.graphics.Color.parseColor("#94a3b8")
            )
        }
    }

    private fun acknowledgeCurrentAlarm() {
        val alarm = currentAlarm ?: return
        val t = token ?: return

        lifecycleScope.launch {
            try {
                val response = ApiProvider.service.acknowledgeAlarm("Bearer $t", alarm.id)
                if (response.isSuccessful) {
                    soundManager?.stopAlarmSound()
                    isAcknowledged = true
                    acknowledgedAlarmId = alarm.id
                    runOnUiThread {
                        // Masquer le bouton
                        findViewById<Button>(R.id.dashboardAckButton).visibility = View.GONE

                        // Afficher statut acquitté
                        val statusText = findViewById<TextView>(R.id.ackStatusText)
                        statusText.text = "\u2705 Acquitt\u00e9e"
                        statusText.visibility = View.VISIBLE

                        // Afficher temps restant (dynamique depuis la réponse)
                        val remainingText = findViewById<TextView>(R.id.ackRemainingTime)
                        val remaining = response.body()?.ack_remaining_seconds ?: 1800
                        val min = remaining / 60
                        remainingText.text = "$min min restantes"
                        remainingText.visibility = View.VISIBLE
                    }
                } else {
                    runOnUiThread {
                        Toast.makeText(this@DashboardActivity,
                            "Échec de l'acquittement : ${response.code()}",
                            Toast.LENGTH_SHORT).show()
                    }
                }
            } catch (e: Exception) {
                runOnUiThread {
                    Toast.makeText(this@DashboardActivity,
                        "Erreur : ${e.message}",
                        Toast.LENGTH_SHORT).show()
                }
            }
        }
    }

    private fun loadHistory() {
        val t = token ?: return
        lifecycleScope.launch {
            try {
                val response = ApiProvider.service.getAlarmHistory("Bearer $t")
                if (response.isSuccessful) {
                    val allAlarms = response.body() ?: emptyList()
                    val pastAlarms = allAlarms.filter { it.status in listOf("resolved", "acknowledged") }
                    runOnUiThread { displayHistory(pastAlarms) }
                }
            } catch (_: Exception) { }
        }
    }

    private fun displayHistory(alarms: List<AlarmResponse>) {
        val section = findViewById<LinearLayout>(R.id.alarmHistorySection)
        val list = findViewById<LinearLayout>(R.id.alarmHistoryList)

        if (alarms.isEmpty()) {
            section.visibility = View.GONE
            return
        }

        section.visibility = View.VISIBLE
        list.removeAllViews()

        for (alarm in alarms) {
            val entry = TextView(this).apply {
                text = "${alarm.title} — ${formatDate(alarm.created_at)} → ${formatDate(alarm.acknowledged_at)}"
                textSize = 13f
                setTextColor(0xFF94a3b8.toInt())
                setPadding(0, 4, 0, 4)
            }
            list.addView(entry)
        }
    }

    private fun computeDuration(createdAt: String): String {
        return try {
            val sdf = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss", Locale.getDefault())
            sdf.timeZone = TimeZone.getTimeZone("UTC")
            val created = sdf.parse(createdAt)
            val diffMs = System.currentTimeMillis() - (created?.time ?: System.currentTimeMillis())
            val minutes = (diffMs / 60000).toInt()
            val hours = minutes / 60
            when {
                hours > 0 -> "${hours}h ${minutes % 60}min"
                minutes > 0 -> "${minutes} min"
                else -> "quelques secondes"
            }
        } catch (_: Exception) {
            "depuis un moment"
        }
    }

    private fun formatDate(dateStr: String?): String {
        if (dateStr == null) return "?"
        return try {
            val sdf = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss", Locale.getDefault())
            val display = SimpleDateFormat("dd/MM HH:mm", Locale.getDefault())
            val date = sdf.parse(dateStr)
            display.format(date!!)
        } catch (_: Exception) {
            dateStr.take(16)
        }
    }

    override fun onDestroy() {
        statusUpdateJob?.cancel()
        historyJob?.cancel()
        soundManager?.stopAlarmSound()
        connectionLostSoundManager?.stopAlarmSound()
        super.onDestroy()
    }
}
