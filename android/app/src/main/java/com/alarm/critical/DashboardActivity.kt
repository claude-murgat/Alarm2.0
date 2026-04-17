package com.alarm.critical

import android.Manifest
import android.animation.ObjectAnimator
import android.animation.ValueAnimator
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.Typeface
import android.os.Build
import android.os.Bundle
import android.view.Gravity
import android.view.View
import android.view.animation.AccelerateDecelerateInterpolator
import android.widget.Button
import android.widget.FrameLayout
import android.widget.ImageButton
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.drawerlayout.widget.DrawerLayout
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import android.util.Log
import com.alarm.critical.api.ApiProvider
import com.alarm.critical.model.AlarmResponse
import com.alarm.critical.model.FcmTokenRequest
import com.alarm.critical.model.FcmTokenDeleteRequest
import com.alarm.critical.service.AlarmPollingService
import com.alarm.critical.service.AlarmSoundManager
import com.alarm.critical.util.AppLogger
import com.alarm.critical.view.ArcTimerView
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
    private var alarmGoneDuringAck = false
    private var acknowledgedAlarmId: Int? = null
    private var pulseAnimator: ObjectAnimator? = null
    private var lastCardState: String = "calm"

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

        AppLogger.log("Dashboard", "Ouvert (user=$userName)")

        // Header : nom + initiale
        findViewById<TextView>(R.id.userNameText).text = userName
        findViewById<TextView>(R.id.userInitial).text = userName.take(1).uppercase()

        // Badge de garde
        updateGuardBadge()

        // Navigation Drawer
        val drawerLayout = findViewById<DrawerLayout>(R.id.drawerLayout)
        findViewById<TextView>(R.id.drawerInitial).text = userName.take(1).uppercase()
        findViewById<TextView>(R.id.drawerUserName).text = userName
        updateDrawerGuardStatus()

        // Enregistrer le token FCM au demarrage
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

        // Demarrer le service de polling
        val isOncall = prefs.getBoolean("is_oncall", true)
        val startedByFcm = prefs.getBoolean("started_by_fcm", false)
        if (isOncall || startedByFcm) {
            startPollingService()
        }

        // Bouton hamburger -> ouvre le drawer
        findViewById<ImageButton>(R.id.menuButton).setOnClickListener {
            drawerLayout.openDrawer(findViewById<LinearLayout>(R.id.drawerContent))
        }

        // Logout dans le drawer
        findViewById<LinearLayout>(R.id.drawerLogout).setOnClickListener {
            drawerLayout.close()
            performLogout()
        }

        // Bouton reconnexion (dans alerte)
        findViewById<Button>(R.id.reconnectButton).setOnClickListener {
            performLogout()
        }

        // Bouton aide flottant — export logs
        findViewById<LinearLayout>(R.id.helpFab).setOnClickListener {
            shareLogs()
        }

        // Bouton acquitter
        findViewById<Button>(R.id.dashboardAckButton).setOnClickListener {
            acknowledgeCurrentAlarm()
        }

        // Mise a jour du statut toutes les secondes
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

    private fun updateGuardBadge() {
        val prefs = getSharedPreferences("alarm_prefs", MODE_PRIVATE)
        val isOncall = prefs.getBoolean("is_oncall", false)
        val escalationPosition = prefs.getInt("escalation_position", -1)
        val badge = findViewById<TextView>(R.id.guardBadge)
        val oncallLabel = findViewById<TextView>(R.id.oncallLabel)

        // Badge escalade : toujours afficher la position si dans la chaine
        if (escalationPosition > 0) {
            badge.text = "ESCALADE n\u00b0$escalationPosition"
            badge.setBackgroundResource(R.drawable.badge_escalation)
            badge.setTextColor(Color.parseColor("#94a3b8"))
            badge.visibility = View.VISIBLE
        } else {
            badge.visibility = View.GONE
        }

        // Etiquette "Actuellement de garde" : uniquement si position 1
        if (isOncall) {
            oncallLabel.visibility = View.VISIBLE
        } else {
            oncallLabel.visibility = View.GONE
        }
    }

    private fun updateDrawerGuardStatus() {
        val prefs = getSharedPreferences("alarm_prefs", MODE_PRIVATE)
        val isOncall = prefs.getBoolean("is_oncall", false)
        val escalationPosition = prefs.getInt("escalation_position", -1)
        val drawerStatus = findViewById<TextView>(R.id.drawerGuardStatus)

        when {
            isOncall -> drawerStatus.text = "Actuellement de garde \u2014 Escalade n\u00b0$escalationPosition"
            escalationPosition > 0 -> drawerStatus.text = "Escalade n\u00b0$escalationPosition"
            else -> drawerStatus.text = "Hors chaine d'escalade"
        }
    }

    private fun performLogout() {
        val prefs = getSharedPreferences("alarm_prefs", MODE_PRIVATE)
        val t = token
        val deviceId = prefs.getString("device_token", null)

        // Supprimer le token FCM cote backend avant de partir
        if (t != null && deviceId != null) {
            lifecycleScope.launch {
                try {
                    ApiProvider.service.deleteFcmToken(
                        "Bearer $t",
                        FcmTokenDeleteRequest(device_id = deviceId)
                    )
                    Log.d("Dashboard", "FCM token deleted from backend")
                } catch (e: Exception) {
                    Log.e("Dashboard", "FCM token delete failed: ${e.message}")
                }
            }
        }

        prefs.edit().clear().apply()
        stopPollingService()
        soundManager?.stopAlarmSound()
        connectionLostSoundManager?.stopAlarmSound()
        startActivity(Intent(this, MainActivity::class.java))
        finish()
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
            // ========== STATUT SYSTEME SIMPLIFIE ==========
            val systemStatus = findViewById<TextView>(R.id.systemStatus)
            val prefs2 = getSharedPreferences("alarm_prefs", MODE_PRIVATE)
            val isOncallNow = prefs2.getBoolean("is_oncall", false)
            val isConnected = AlarmPollingService.isRunning && AlarmPollingService.lastHeartbeatOk

            when {
                isConnected -> {
                    systemStatus.text = "\u25CF Connecte au serveur"
                    systemStatus.setTextColor(Color.parseColor("#22c55e"))
                }
                !AlarmPollingService.isRunning && !isOncallNow -> {
                    // Non-astreinte sans polling actif = en attente de push
                    systemStatus.text = "\u25CF En attente"
                    systemStatus.setTextColor(Color.parseColor("#f59e0b"))
                }
                else -> {
                    systemStatus.text = "\u25CF Deconnecte"
                    systemStatus.setTextColor(Color.parseColor("#ef4444"))
                }
            }

            // ========== ALERTE CONNEXION / AUTH ==========
            val connectionLostContainer = findViewById<LinearLayout>(R.id.connectionLostContainer)
            val connectionLostAlert = findViewById<TextView>(R.id.connectionLostAlert)
            val reconnectButton = findViewById<Button>(R.id.reconnectButton)

            if (AlarmPollingService.authErrorAlarm) {
                connectionLostAlert.text = AlarmPollingService.authErrorMessage ?: "Session expiree"
                connectionLostContainer.visibility = View.VISIBLE
                reconnectButton.visibility = View.VISIBLE
                if (connectionLostSoundManager == null) {
                    connectionLostSoundManager = AlarmSoundManager(this@DashboardActivity)
                }
                connectionLostSoundManager?.startAlarmSound()
            } else if (AlarmPollingService.heartbeatLostAlarm) {
                connectionLostAlert.text = "Connexion perdue avec le serveur"
                connectionLostContainer.visibility = View.VISIBLE
                reconnectButton.visibility = View.GONE
                if (connectionLostSoundManager == null) {
                    connectionLostSoundManager = AlarmSoundManager(this@DashboardActivity)
                }
                connectionLostSoundManager?.startAlarmSound()
            } else {
                connectionLostContainer.visibility = View.GONE
                connectionLostSoundManager?.stopAlarmSound()
            }

            // ========== BADGE DE GARDE (rafraichi depuis prefs, mis a jour par FCM) ==========
            updateGuardBadge()

            // ========== CARTE ALARME HERO ==========
            val alarm = AlarmPollingService.currentAlarm
            val alarmCard = findViewById<LinearLayout>(R.id.alarmCard)
            val alarmDot = findViewById<View>(R.id.alarmDot)
            val alarmLine = findViewById<TextView>(R.id.currentAlarmLine)
            val titleView = findViewById<TextView>(R.id.alarmTitle)
            val messageView = findViewById<TextView>(R.id.alarmMessage)
            val durationView = findViewById<TextView>(R.id.alarmDuration)
            val ackButton = findViewById<Button>(R.id.dashboardAckButton)
            val ackStatus = findViewById<TextView>(R.id.ackStatusText)
            val arcTimer = findViewById<ArcTimerView>(R.id.ackArcTimer)

            // Reset ack si une AUTRE alarme arrive (ID different)
            if (alarm != null && isAcknowledged && alarm.id != acknowledgedAlarmId) {
                isAcknowledged = false
                alarmGoneDuringAck = false
                acknowledgedAlarmId = null
            }

            // Si l'alarme revient apres avoir disparu pendant l'ack
            if (alarm != null && isAcknowledged && alarmGoneDuringAck) {
                isAcknowledged = false
                alarmGoneDuringAck = false
            }

            // Reset ack quand le backend remet l'alarme en "active" apres expiry
            if (alarm != null && isAcknowledged && alarm.id == acknowledgedAlarmId
                && alarm.status != "acknowledged") {
                isAcknowledged = false
                alarmGoneDuringAck = false
                acknowledgedAlarmId = null
            }

            if (alarm != null && alarm.status == "acknowledged" && !isAcknowledged) {
                // Alarme acquittee par quelqu'un d'autre
                setCardState(alarmCard, alarmDot, "acked")
                currentAlarm = alarm
                val ackerName = alarm.acknowledged_by_name ?: "?"
                alarmLine.text = "Alarme active"
                alarmLine.setTextColor(Color.parseColor("#22c55e"))

                titleView.text = alarm.title
                titleView.visibility = View.VISIBLE
                messageView.text = alarm.message
                messageView.visibility = View.VISIBLE

                val duration = computeDuration(alarm.created_at)
                durationView.text = "Active depuis $duration"
                durationView.visibility = View.VISIBLE

                ackButton.visibility = View.GONE
                ackStatus.text = "Acquittee par $ackerName"
                ackStatus.visibility = View.VISIBLE

                val remaining = alarm.ack_remaining_seconds ?: 0
                arcTimer.setTime(1800, remaining)
                arcTimer.visibility = View.VISIBLE

                soundManager?.stopAlarmSound()

            } else if (alarm != null && !isAcknowledged) {
                // Alarme active non acquittee
                setCardState(alarmCard, alarmDot, "active")
                currentAlarm = alarm
                alarmLine.text = "Alarme active"
                alarmLine.setTextColor(Color.parseColor("#ef4444"))

                titleView.text = alarm.title
                titleView.visibility = View.VISIBLE
                messageView.text = alarm.message
                messageView.visibility = View.VISIBLE

                val duration = computeDuration(alarm.created_at)
                durationView.text = "Active depuis $duration"
                durationView.visibility = View.VISIBLE

                ackButton.visibility = View.VISIBLE
                ackStatus.visibility = View.GONE
                arcTimer.visibility = View.GONE

                if (soundManager == null) {
                    soundManager = AlarmSoundManager(this)
                }
                soundManager?.startAlarmSound()

            } else if (alarm != null && isAcknowledged) {
                // Alarme acquittee par nous
                setCardState(alarmCard, alarmDot, "acked")
                currentAlarm = alarm
                alarmLine.text = "Acquittee"
                alarmLine.setTextColor(Color.parseColor("#22c55e"))

                titleView.text = alarm.title
                titleView.visibility = View.VISIBLE
                messageView.text = alarm.message
                messageView.visibility = View.VISIBLE

                val duration = computeDuration(alarm.created_at)
                durationView.text = "Active depuis $duration"
                durationView.visibility = View.VISIBLE

                ackButton.visibility = View.GONE

                val remaining = alarm.ack_remaining_seconds ?: 0
                arcTimer.setTime(1800, remaining)
                arcTimer.visibility = View.VISIBLE

            } else if (alarm == null && isAcknowledged) {
                // Alarme disparue pendant ack
                setCardState(alarmCard, alarmDot, "calm")
                alarmGoneDuringAck = true
                alarmLine.text = "Tout est calme"
                alarmLine.setTextColor(Color.parseColor("#94a3b8"))
                titleView.visibility = View.GONE
                messageView.visibility = View.GONE
                durationView.visibility = View.GONE
                ackButton.visibility = View.GONE
                arcTimer.visibility = View.GONE

                soundManager?.stopAlarmSound()

            } else {
                // Pas d'alarme
                setCardState(alarmCard, alarmDot, "calm")
                currentAlarm = null
                isAcknowledged = false
                alarmLine.text = "Tout est calme"
                alarmLine.setTextColor(Color.parseColor("#94a3b8"))

                titleView.visibility = View.GONE
                messageView.visibility = View.GONE
                durationView.visibility = View.GONE
                ackButton.visibility = View.GONE
                arcTimer.visibility = View.GONE

                soundManager?.stopAlarmSound()

                ackStatus.visibility = View.GONE
            }

            // ========== CHIP SONNERIE (visible uniquement quand active) ==========
            val isSounding = (soundManager?.isPlaying() == true) ||
                             (connectionLostSoundManager?.isPlaying() == true)
            val soundChip = findViewById<LinearLayout>(R.id.soundChip)
            if (isSounding) {
                soundChip.visibility = View.VISIBLE
                findViewById<TextView>(R.id.soundStatus).text = "Sonnerie ACTIVE"
                findViewById<TextView>(R.id.soundStatus).setTextColor(Color.parseColor("#ef4444"))
                findViewById<View>(R.id.soundDot).setBackgroundResource(R.drawable.dot_red)
            } else {
                soundChip.visibility = View.GONE
            }
        }
    }

    private fun setCardState(card: LinearLayout, dot: View, state: String) {
        if (state == lastCardState) return
        lastCardState = state

        pulseAnimator?.cancel()
        pulseAnimator = null

        when (state) {
            "calm" -> {
                card.setBackgroundResource(R.drawable.card_calm)
                dot.setBackgroundResource(R.drawable.dot_green)
            }
            "active" -> {
                card.setBackgroundResource(R.drawable.card_alarm_active)
                dot.setBackgroundResource(R.drawable.dot_red)
                pulseAnimator = ObjectAnimator.ofFloat(card, "alpha", 1f, 0.85f).apply {
                    duration = 800
                    repeatMode = ValueAnimator.REVERSE
                    repeatCount = ValueAnimator.INFINITE
                    interpolator = AccelerateDecelerateInterpolator()
                    start()
                }
            }
            "acked" -> {
                card.setBackgroundResource(R.drawable.card_alarm_acked)
                dot.setBackgroundResource(R.drawable.dot_green)
            }
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
                        findViewById<Button>(R.id.dashboardAckButton).visibility = View.GONE

                        val statusText = findViewById<TextView>(R.id.ackStatusText)
                        statusText.text = "Acquittee"
                        statusText.visibility = View.VISIBLE

                        val remaining = response.body()?.ack_remaining_seconds ?: 1800
                        val arcTimer = findViewById<ArcTimerView>(R.id.ackArcTimer)
                        arcTimer.setTime(1800, remaining)
                        arcTimer.visibility = View.VISIBLE
                    }
                } else {
                    runOnUiThread {
                        Toast.makeText(this@DashboardActivity,
                            "Echec de l'acquittement : ${response.code()}",
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

        for ((index, alarm) in alarms.withIndex()) {
            val row = LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL
                layoutParams = LinearLayout.LayoutParams(
                    LinearLayout.LayoutParams.MATCH_PARENT,
                    LinearLayout.LayoutParams.WRAP_CONTENT
                )
            }

            val timelineCol = FrameLayout(this).apply {
                layoutParams = LinearLayout.LayoutParams(24.dpToPx(), LinearLayout.LayoutParams.MATCH_PARENT)
            }

            val dotView = View(this).apply {
                val dotSize = 8.dpToPx()
                layoutParams = FrameLayout.LayoutParams(dotSize, dotSize).apply {
                    gravity = Gravity.TOP or Gravity.CENTER_HORIZONTAL
                    topMargin = 6.dpToPx()
                }
                setBackgroundResource(
                    when (alarm.status) {
                        "acknowledged" -> R.drawable.history_dot_green
                        "resolved" -> R.drawable.history_dot_gray
                        else -> R.drawable.history_dot_red
                    }
                )
            }
            timelineCol.addView(dotView)

            if (index < alarms.size - 1) {
                val lineView = View(this).apply {
                    layoutParams = FrameLayout.LayoutParams(2.dpToPx(), FrameLayout.LayoutParams.MATCH_PARENT).apply {
                        gravity = Gravity.CENTER_HORIZONTAL
                        topMargin = 18.dpToPx()
                    }
                    setBackgroundResource(R.drawable.history_line)
                }
                timelineCol.addView(lineView)
            }
            row.addView(timelineCol)

            val contentCol = LinearLayout(this).apply {
                orientation = LinearLayout.VERTICAL
                layoutParams = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f).apply {
                    marginStart = 8.dpToPx()
                }
                setPadding(0, 0, 0, 16.dpToPx())
            }

            val titleText = TextView(this).apply {
                text = alarm.title
                textSize = 14f
                setTextColor(Color.WHITE)
                typeface = Typeface.DEFAULT_BOLD
            }
            contentCol.addView(titleText)

            val timeText = TextView(this).apply {
                val created = formatDate(alarm.created_at)
                val acked = if (alarm.acknowledged_at != null) {
                    val ackerName = alarm.acknowledged_by_name ?: ""
                    " — acquittee par $ackerName"
                } else {
                    " — resolue"
                }
                text = "$created$acked"
                textSize = 12f
                setTextColor(Color.parseColor("#94a3b8"))
            }
            contentCol.addView(timeText)

            row.addView(contentCol)
            list.addView(row)
        }
    }

    private fun shareLogs() {
        val prefs = getSharedPreferences("alarm_prefs", MODE_PRIVATE)
        val userName = prefs.getString("user_name", "?") ?: "?"
        val version = try { packageManager.getPackageInfo(packageName, 0).versionName } catch (_: Exception) { "?" }
        val logs = AppLogger.exportLogs(userName, version ?: "?")

        val intent = Intent(Intent.ACTION_SEND).apply {
            type = "text/plain"
            putExtra(Intent.EXTRA_SUBJECT, "Alarme Murgat - Logs de diagnostic")
            putExtra(Intent.EXTRA_TEXT, logs)
        }
        startActivity(Intent.createChooser(intent, "Envoyer les logs via..."))
    }

    private fun Int.dpToPx(): Int = (this * resources.displayMetrics.density).toInt()

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
        pulseAnimator?.cancel()
        soundManager?.stopAlarmSound()
        connectionLostSoundManager?.stopAlarmSound()
        super.onDestroy()
    }
}
