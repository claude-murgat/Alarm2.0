package com.alarm.critical.view

import android.animation.ValueAnimator
import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.RectF
import android.util.AttributeSet
import android.view.View
import com.alarm.critical.model.ModemStatusResponse

/**
 * INV-ANDROID-110 : icone 4 barres du signal 4G du modem gateway (INV-129).
 * Vert (orange a 1 barre) avec pulsation lente ; rouge pulse vite sans reseau ;
 * gris si etat inconnu. Le libelle (contentDescription) porte techno et dBm.
 */
class SignalBarsView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
    defStyleAttr: Int = 0
) : View(context, attrs, defStyleAttr) {

    private var state = "unknown"
    private var bars = 0
    private val paint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val rect = RectF()
    private var pulse: ValueAnimator? = null

    init {
        contentDescription = label(null)
    }

    fun show(status: ModemStatusResponse?) {
        val newState = status?.state ?: "unknown"
        val newBars = status?.bars ?: 0
        contentDescription = label(status)
        if (newState == state && newBars == bars) return
        state = newState
        bars = newBars
        restartPulse()
        invalidate()
    }

    private fun label(s: ModemStatusResponse?): String = when (s?.state) {
        "no_network" -> "Modem sans réseau : SMS et appels de secours impossibles"
        "ok" -> "Signal ${s.tech ?: ""} du modem : ${s.bars}/4" + (s.rsrp?.let { " ($it dBm)" } ?: "")
        else -> "Signal du modem inconnu"
    }

    private fun restartPulse() {
        pulse?.cancel()
        alpha = 1f
        if (state == "unknown") return
        pulse = ValueAnimator.ofFloat(1f, 0.45f).apply {
            duration = if (state == "no_network") 500L else 1200L
            repeatMode = ValueAnimator.REVERSE
            repeatCount = ValueAnimator.INFINITE
            addUpdateListener { alpha = it.animatedValue as Float }
            start()
        }
    }

    override fun onDetachedFromWindow() {
        pulse?.cancel()
        super.onDetachedFromWindow()
    }

    override fun onDraw(canvas: Canvas) {
        val gap = width * 0.08f
        val barW = (width - 3 * gap) / 4
        val on = when {
            state == "no_network" -> Color.parseColor("#ef4444")
            state == "ok" && bars <= 1 -> Color.parseColor("#f59e0b")
            state == "ok" -> Color.parseColor("#22c55e")
            else -> Color.parseColor("#475569")
        }
        for (i in 0 until 4) {
            val left = i * (barW + gap)
            val top = height * (1 - (i + 1) / 4f)
            rect.set(left, top, left + barW, height.toFloat())
            // Sans reseau : les 4 barres en rouge pour que l'alerte se voie.
            paint.color = if (state == "no_network" || i < bars) on else Color.parseColor("#334155")
            canvas.drawRoundRect(rect, 2f, 2f, paint)
        }
    }
}
