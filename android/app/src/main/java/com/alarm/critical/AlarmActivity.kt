package com.alarm.critical

import android.animation.ArgbEvaluator
import android.animation.ObjectAnimator
import android.animation.ValueAnimator
import android.os.Bundle
import android.view.View
import android.view.WindowManager
import android.view.animation.AccelerateDecelerateInterpolator
import android.widget.Button
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import com.alarm.critical.api.ApiProvider
import com.alarm.critical.service.AlarmSoundManager
import com.alarm.critical.view.ArcTimerView
import kotlinx.coroutines.launch

class AlarmActivity : AppCompatActivity() {
    companion object {
        @Volatile
        var isVisible = false
    }

    private lateinit var soundManager: AlarmSoundManager
    private var alarmId: Int = 0
    private var token: String? = null
    private var bgPulseAnimator: ValueAnimator? = null
    private var iconPulseAnimator: ObjectAnimator? = null
    private var iconPulseAnimatorY: ObjectAnimator? = null

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
        token = intent.getStringExtra("token")
            ?: getSharedPreferences("alarm_prefs", MODE_PRIVATE).getString("token", null)

        findViewById<TextView>(R.id.alarmTitle).text = title
        findViewById<TextView>(R.id.alarmMessage).text = message
        findViewById<TextView>(R.id.alarmId).text = "n\u00b0$alarmId"

        // Start alarm sound
        soundManager = AlarmSoundManager(this)
        soundManager.startAlarmSound()

        // Start pulsation animations
        startPulseAnimations()

        // Acknowledge button
        findViewById<Button>(R.id.ackButton).setOnClickListener {
            acknowledgeAlarm()
        }

        // Back to dashboard button
        findViewById<Button>(R.id.backToDashboard).setOnClickListener {
            finish()
        }
    }

    private fun startPulseAnimations() {
        val root = findViewById<FrameLayout>(R.id.alarmRoot)
        val colorFrom = 0xFFB71C1C.toInt()
        val colorTo = 0xFFD32F2F.toInt()
        bgPulseAnimator = ValueAnimator.ofObject(ArgbEvaluator(), colorFrom, colorTo).apply {
            duration = 1000
            repeatMode = ValueAnimator.REVERSE
            repeatCount = ValueAnimator.INFINITE
            addUpdateListener { animator ->
                root.setBackgroundColor(animator.animatedValue as Int)
            }
            start()
        }

        val icon = findViewById<TextView>(R.id.alarmIcon)
        iconPulseAnimator = ObjectAnimator.ofFloat(icon, "scaleX", 1f, 1.2f).apply {
            duration = 600
            repeatMode = ValueAnimator.REVERSE
            repeatCount = ValueAnimator.INFINITE
            interpolator = AccelerateDecelerateInterpolator()
            start()
        }
        iconPulseAnimatorY = ObjectAnimator.ofFloat(icon, "scaleY", 1f, 1.2f).apply {
            duration = 600
            repeatMode = ValueAnimator.REVERSE
            repeatCount = ValueAnimator.INFINITE
            interpolator = AccelerateDecelerateInterpolator()
            start()
        }
    }

    private fun stopPulseAnimations() {
        bgPulseAnimator?.cancel()
        iconPulseAnimator?.cancel()
        iconPulseAnimatorY?.cancel()
    }

    private fun transitionToAcknowledged(remainingSeconds: Int) {
        stopPulseAnimations()

        // Transition background rouge -> vert
        val root = findViewById<FrameLayout>(R.id.alarmRoot)
        val colorFrom = 0xFFB71C1C.toInt()
        val colorTo = 0xFF1B5E20.toInt()
        ValueAnimator.ofObject(ArgbEvaluator(), colorFrom, colorTo).apply {
            duration = 600
            addUpdateListener { animator ->
                root.setBackgroundColor(animator.animatedValue as Int)
            }
            start()
        }

        // Masquer bouton + hint
        findViewById<LinearLayout>(R.id.bottomSection).visibility = View.GONE

        // Afficher confirmation avec ArcTimer
        val confirmation = findViewById<LinearLayout>(R.id.ackConfirmation)
        confirmation.visibility = View.VISIBLE
        confirmation.alpha = 0f
        confirmation.animate().alpha(1f).setDuration(400).start()

        val arcTimer = findViewById<ArcTimerView>(R.id.ackArcTimer)
        arcTimer.setTime(1800, remainingSeconds)
    }

    private fun acknowledgeAlarm() {
        if (token == null) {
            Toast.makeText(this, "Non authentifie", Toast.LENGTH_SHORT).show()
            return
        }

        lifecycleScope.launch {
            try {
                val response = ApiProvider.service.acknowledgeAlarm("Bearer $token", alarmId)
                if (response.isSuccessful) {
                    soundManager.stopAlarmSound()
                    val remaining = response.body()?.ack_remaining_seconds ?: 1800
                    runOnUiThread {
                        transitionToAcknowledged(remaining)
                    }
                } else {
                    runOnUiThread {
                        Toast.makeText(
                            this@AlarmActivity,
                            "Echec de l'acquittement : ${response.code()}",
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

    override fun onResume() {
        super.onResume()
        isVisible = true
    }

    override fun onPause() {
        super.onPause()
        isVisible = false
    }

    override fun onDestroy() {
        isVisible = false
        stopPulseAnimations()
        soundManager.stopAlarmSound()
        super.onDestroy()
    }

    override fun onBackPressed() {
        // Prevent dismissing alarm with back button
        // User must acknowledge
    }
}
