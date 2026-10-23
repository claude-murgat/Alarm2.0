package com.alarm.critical

import com.alarm.critical.service.AlarmPollingService

import android.animation.ArgbEvaluator
import android.animation.ObjectAnimator
import android.animation.ValueAnimator
import android.content.res.ColorStateList
import android.graphics.Color
import android.graphics.Typeface
import android.os.Bundle
import android.view.Gravity
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
import com.alarm.critical.model.AlarmResponse
import com.alarm.critical.service.AlarmSoundManager
import com.alarm.critical.view.ArcTimerView
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

class AlarmActivity : AppCompatActivity() {
    companion object {
        @Volatile
        var isVisible = false

        // INV-ANDROID-210 : ecran ouvert, pour le fermer quand l'alarme est terminee.
        @Volatile
        private var shown: java.lang.ref.WeakReference<AlarmActivity>? = null

        /** Ferme l'ecran d'alarme (onDestroy coupe sa sonnerie). alarmId null : quelle qu'elle soit. */
        fun closeIfShowing(alarmId: Int? = null) {
            val activity = shown?.get() ?: return
            activity.runOnUiThread {
                if (alarmId == null || activity.alarmId == alarmId) activity.finish()
            }
        }
    }

    private lateinit var soundManager: AlarmSoundManager
    private var alarmId: Int = 0
    private var token: String? = null
    private var bgPulseAnimator: ValueAnimator? = null
    private var iconPulseAnimator: ObjectAnimator? = null
    private var iconPulseAnimatorY: ObjectAnimator? = null
    // null : rien d'affiche encore ; sinon dernier etat (toutes acquittees ou non)
    private var allAcked: Boolean? = null
    private var listKey = ""

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
        shown = java.lang.ref.WeakReference(this)
        token = intent.getStringExtra("token")
            ?: getSharedPreferences("alarm_prefs", MODE_PRIVATE).getString("token", null)

        soundManager = AlarmSoundManager(this)

        // Acknowledge button (une seule alarme)
        findViewById<Button>(R.id.ackButton).setOnClickListener {
            acknowledgeAlarm(alarmId)
        }

        // Back to dashboard button
        findViewById<Button>(R.id.backToDashboard).setOnClickListener {
            finish()
        }

        // Avant le 1er poll (ecran ouvert par la notif, service pas encore a jour) : l'alarme de l'intent.
        // show() demarre la sonnerie et la pulsation.
        show(AlarmPollingService.alarms.ifEmpty {
            listOf(AlarmResponse(
                id = alarmId,
                title = intent.getStringExtra("alarm_title") ?: "ALARME CRITIQUE",
                message = intent.getStringExtra("alarm_message") ?: "",
                severity = "critical", status = "active", assigned_user_id = null,
                acknowledged_at = null, acknowledged_by_name = null, suspended_until = null,
                escalation_count = 0, created_at = "", site = intent.getStringExtra("alarm_site")
            ))
        })
        // INV-ANDROID-009 : suivre la liste du service (alarme d'un autre site, acquittement, resolution).
        lifecycleScope.launch {
            while (isActive) {
                delay(1000)
                show(AlarmPollingService.alarms)
            }
        }
    }

    /**
     * INV-ANDROID-009 : une alarme -> son site en gros + bouton du bas ; deux ou plus -> une ligne
     * par alarme avec son propre bouton. Sonne tant qu'une alarme reste a acquitter.
     */
    private fun show(alarms: List<AlarmResponse>) {
        if (alarms.isEmpty()) return  // alarme terminee : le service ferme l'ecran (INV-ANDROID-210)
        val multi = alarms.size >= 2
        val first = alarms.first()
        alarmId = first.id
        findViewById<TextView>(R.id.alarmSite).text = if (multi) "${alarms.size} alarmes" else first.siteName
        findViewById<TextView>(R.id.alarmId).text = if (multi) "" else "n\u00b0${first.id}"
        findViewById<TextView>(R.id.alarmTitle).apply {
            text = first.title
            visibility = if (multi) View.GONE else View.VISIBLE
        }
        findViewById<TextView>(R.id.alarmMessage).apply {
            text = first.message
            visibility = if (multi) View.GONE else View.VISIBLE
        }
        val key = if (multi) alarms.joinToString { "${it.id}:${it.status}" } else ""
        if (key != listKey) {
            listKey = key
            fillAlarmList(if (multi) alarms else emptyList())
        }

        val nowAcked = alarms.none { it.status != "acknowledged" }
        if (nowAcked && allAcked != true) {
            soundManager.stopAlarmSound()
            transitionToAcknowledged(alarms.mapNotNull { it.ack_remaining_seconds }.minOrNull() ?: 1800)
        } else if (!nowAcked && allAcked != false) {
            // Ouverture, ou nouvelle alarme (autre site) apres acquittement : retour au rouge
            findViewById<LinearLayout>(R.id.ackConfirmation).visibility = View.GONE
            startPulseAnimations()
            soundManager.startAlarmRing()
        }
        allAcked = nowAcked
        findViewById<LinearLayout>(R.id.bottomSection).visibility =
            if (multi || nowAcked) View.GONE else View.VISIBLE
    }

    private fun fillAlarmList(alarms: List<AlarmResponse>) {
        val list = findViewById<LinearLayout>(R.id.alarmList)
        list.removeAllViews()
        list.visibility = if (alarms.isEmpty()) View.GONE else View.VISIBLE
        val pad = (12 * resources.displayMetrics.density).toInt()
        for (a in alarms) {
            val row = LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = Gravity.CENTER_VERTICAL
                setPadding(pad, pad, pad, pad)
                setBackgroundColor(0x40000000)
            }
            val texts = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
            texts.addView(TextView(this).apply {
                text = a.siteName
                textSize = 26f
                setTextColor(Color.WHITE)
                setTypeface(typeface, Typeface.BOLD)
            })
            texts.addView(TextView(this).apply {
                text = a.title
                textSize = 14f
                setTextColor(0xFFFFEBEE.toInt())
            })
            row.addView(texts, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
            row.addView(
                if (a.status == "acknowledged") TextView(this).apply {
                    text = "Acquittee"
                    textSize = 16f
                    setTextColor(0xFFA5D6A7.toInt())
                    setTypeface(typeface, Typeface.BOLD)
                } else Button(this).apply {
                    text = "ACQUITTER"
                    contentDescription = "Acquitter ${a.siteName}"
                    backgroundTintList = ColorStateList.valueOf(0xFF4CAF50.toInt())
                    setTextColor(Color.WHITE)
                    setOnClickListener { acknowledgeAlarm(a.id) }
                }
            )
            list.addView(row, LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT
            ).apply { bottomMargin = pad / 2 })
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

    private fun acknowledgeAlarm(id: Int) {
        if (token == null) {
            Toast.makeText(this, "Non authentifie", Toast.LENGTH_SHORT).show()
            return
        }

        lifecycleScope.launch {
            try {
                val response = ApiProvider.service.acknowledgeAlarm("Bearer $token", id)
                if (response.isSuccessful) {
                    AlarmPollingService.onAckSucceeded(response.body())
                    // Coupe la sonnerie et passe au vert seulement si plus rien n'est a acquitter
                    show(AlarmPollingService.alarms)
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
        if (shown?.get() === this) shown = null
        stopPulseAnimations()
        soundManager.stopAlarmSound()
        super.onDestroy()
    }

    override fun onBackPressed() {
        // Prevent dismissing alarm with back button
        // User must acknowledge
    }
}
