package com.alarm.critical.view

import android.content.Context
import android.graphics.*
import android.util.AttributeSet
import android.view.View

/**
 * Composant graphique affichant un arc de cercle qui se vide
 * pour representer le temps restant apres acquittement.
 *
 * Usage : setTime(maxSeconds, remainingSeconds)
 * L'arc vert se reduit au fur et a mesure que le temps passe.
 * Le texte "XX min" est affiche au centre.
 */
class ArcTimerView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
    defStyleAttr: Int = 0
) : View(context, attrs, defStyleAttr) {

    private var maxSeconds: Int = 1800  // 30 min par defaut
    private var remainingSeconds: Int = 1800
    private var centerText: String = "30 min"

    private val bgArcPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.parseColor("#334155")
        style = Paint.Style.STROKE
        strokeWidth = 8f
        strokeCap = Paint.Cap.ROUND
    }

    private val fgArcPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.parseColor("#22c55e")
        style = Paint.Style.STROKE
        strokeWidth = 8f
        strokeCap = Paint.Cap.ROUND
    }

    private val textPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.WHITE
        textAlign = Paint.Align.CENTER
        typeface = Typeface.DEFAULT_BOLD
    }

    private val labelPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.parseColor("#94a3b8")
        textAlign = Paint.Align.CENTER
    }

    private val rect = RectF()

    fun setTime(max: Int, remaining: Int) {
        maxSeconds = max.coerceAtLeast(1)
        remainingSeconds = remaining.coerceIn(0, maxSeconds)
        val min = remainingSeconds / 60
        centerText = "${min} min"

        // Couleur change quand il reste peu de temps
        val ratio = remainingSeconds.toFloat() / maxSeconds
        fgArcPaint.color = when {
            ratio > 0.3f -> Color.parseColor("#22c55e")  // vert
            ratio > 0.1f -> Color.parseColor("#f59e0b")  // orange
            else -> Color.parseColor("#ef4444")           // rouge
        }
        invalidate()
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)

        val cx = width / 2f
        val cy = height / 2f
        val strokeHalf = bgArcPaint.strokeWidth / 2f
        val padding = strokeHalf + 4f
        val radius = (minOf(width, height) / 2f) - padding

        rect.set(cx - radius, cy - radius, cx + radius, cy + radius)

        // Fond gris (cercle complet)
        canvas.drawArc(rect, -90f, 360f, false, bgArcPaint)

        // Arc vert proportionnel au temps restant
        val sweepAngle = if (maxSeconds > 0) {
            360f * remainingSeconds / maxSeconds
        } else 0f
        canvas.drawArc(rect, -90f, sweepAngle, false, fgArcPaint)

        // Texte central : "XX min"
        textPaint.textSize = radius * 0.45f
        val textY = cy + textPaint.textSize * 0.35f - 4f
        canvas.drawText(centerText, cx, textY, textPaint)

        // Label "restantes"
        labelPaint.textSize = radius * 0.22f
        canvas.drawText("restantes", cx, textY + labelPaint.textSize + 4f, labelPaint)
    }

    override fun onMeasure(widthMeasureSpec: Int, heightMeasureSpec: Int) {
        val desiredSize = (80 * resources.displayMetrics.density).toInt()
        val width = resolveSize(desiredSize, widthMeasureSpec)
        val height = resolveSize(desiredSize, heightMeasureSpec)
        val size = minOf(width, height)
        setMeasuredDimension(size, size)
    }
}
