package dev.nudgy.android

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Path
import android.graphics.RadialGradient
import android.graphics.RectF
import android.graphics.Shader
import android.view.View
import java.time.LocalTime
import kotlin.math.abs
import kotlin.math.max
import kotlin.math.min
import kotlin.math.sin

/** The round pet, drawn like the laptop one: 84×84 units, scaled to the view. */
class PetView(context: Context) : View(context) {
    var pressing = 0
    var alarm = false
    var stale = false
    private var action = ""
    private var actionUntil = 0L
    private var phase = 0f
    private var blinkUntil = 0L

    /** A short reaction: "hop", "cheer", "surprised", "wink", "yawn", "love". */
    fun act(name: String, millis: Long) {
        action = name
        actionUntil = System.currentTimeMillis() + millis
    }

    fun face(): String {
        if (System.currentTimeMillis() < actionUntil) return action
        if (alarm) return "alarm"
        if (pressing > 0) return "busy"
        val hour = LocalTime.now().hour
        return if (stale || hour >= 23 || hour < 7) "sleep" else "happy"
    }

    override fun performClick(): Boolean = super.performClick()

    /** Called about ten times a second while the screen is on. */
    fun tick() {
        phase += 0.1f
        val now = System.currentTimeMillis()
        if (now > blinkUntil + 3000 && Math.random() < 0.02) blinkUntil = now + 150
        invalidate()
    }

    private val fill = Paint(Paint.ANTI_ALIAS_FLAG)
    private val line = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeCap = Paint.Cap.ROUND
    }
    private val text = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        textAlign = Paint.Align.CENTER
        isFakeBoldText = true
    }
    private val ink = Color.rgb(33, 38, 46)
    private val oval = RectF()
    private val body = RectF(-25f, -27f, 25f, 27f)
    private val shades = HashMap<String, Shader>()

    private fun rgb(r: Float, g: Float, b: Float) =
        Color.rgb((min(1f, r) * 255).toInt(), (min(1f, g) * 255).toInt(), (min(1f, b) * 255).toInt())

    private fun colour(face: String): FloatArray = when (face) {
        "busy" -> floatArrayOf(1f, .83f, .47f)
        "alarm" -> floatArrayOf(1f, .54f, .50f)
        "sleep" -> floatArrayOf(.62f, .68f, .85f)
        "love" -> floatArrayOf(1f, .70f, .80f)
        "surprised" -> floatArrayOf(.55f, .85f, .95f)
        else -> floatArrayOf(.49f, .88f, .71f)
    }

    /** Body offset and squash: dx, dy, scaleX, scaleY. */
    private fun motion(face: String): FloatArray {
        val progress = 1f - max(0f, (actionUntil - System.currentTimeMillis()) / 600f)
        return when (face) {
            "cheer", "hop" -> {
                val lift = if (face == "cheer") abs(sin(phase * 3)) else sin(min(1f, progress) * Math.PI.toFloat())
                val squash = 1 - lift
                floatArrayOf(0f, -lift * 9, 1 + .06f * squash, 1 - .06f * squash)
            }
            "busy" -> floatArrayOf(sin(phase * .9f) * 6, abs(sin(phase * 1.8f)) * -1.5f, 1f, 1f)
            "alarm" -> {
                val lift = abs(sin(phase * 2.6f))
                floatArrayOf(sin(phase * 14) * .8f, -lift * 6, 1 + .05f * (1 - lift), 1 - .05f * (1 - lift))
            }
            "sleep" -> {
                val breath = sin(phase * .8f)
                floatArrayOf(0f, 2f, 1 + .02f * breath, 1 + .03f * breath)
            }
            "love" -> floatArrayOf(sin(phase * 2) * 2.5f, sin(phase * 4), 1f, 1f)
            "surprised" -> floatArrayOf(0f, -3f, .96f, 1.05f)
            else -> floatArrayOf(0f, sin(phase) * 1.5f, 1f, 1f)
        }
    }

    override fun onDraw(canvas: Canvas) {
        val face = face()
        val (dx, dy, sx, sy) = motion(face).let { listOf(it[0], it[1], it[2], it[3]) }
        val (r, g, b) = colour(face).let { listOf(it[0], it[1], it[2]) }
        canvas.save()
        canvas.scale(width / 84f, height / 84f)
        val cx = 42 + dx
        val cy = 47 + dy
        val radius = 25f

        // shadow that shrinks as it lifts off
        fill.shader = null
        fill.color = Color.argb(46, 0, 0, 0)
        val shadow = max(8f, 19 + dy * .8f)
        oval.set(42 + dx - shadow, 77 - shadow * .22f, 42 + dx + shadow, 77 + shadow * .22f)
        canvas.drawOval(oval, fill)

        // body, squashed from its base
        canvas.save()
        canvas.translate(cx, cy + radius)
        canvas.scale(sx, sy)
        canvas.translate(0f, -radius)
        fill.color = Color.WHITE                       // full opacity again after the shadow
        fill.shader = shades.getOrPut(face) {
            RadialGradient(-8f, -12f, 31f, rgb(r + .15f, g + .15f, b + .15f),
                rgb(r * .85f, g * .85f, b * .85f), Shader.TileMode.CLAMP)
        }
        canvas.drawOval(body, fill)
        fill.shader = null
        line.color = rgb(r * .55f, g * .55f, b * .55f)
        line.strokeWidth = 1.6f
        canvas.drawOval(body, line)

        drawFace(canvas, face, r, g, b)
        canvas.restore()
        drawExtras(canvas, face, cx, cy)
        if (pressing > 0) drawBadge(canvas)
        canvas.restore()
    }

    private fun drawFace(canvas: Canvas, face: String, r: Float, g: Float, b: Float) {
        val eyeY = -4f
        val blink = System.currentTimeMillis() < blinkUntil && face !in setOf("sleep", "cheer", "love", "yawn")
        fill.color = ink
        line.color = ink
        line.strokeWidth = 2f
        for (x in floatArrayOf(-9f, 9f)) {
            when {
                blink -> canvas.drawLine(x - 3.5f, eyeY, x + 3.5f, eyeY, line)
                face == "sleep" || face == "yawn" ->
                    canvas.drawArc(RectF(x - 3.5f, eyeY - 4.5f, x + 3.5f, eyeY + 2.5f), 27f, 126f, false, line)
                face == "cheer" ->
                    canvas.drawArc(RectF(x - 3.5f, eyeY - 1.5f, x + 3.5f, eyeY + 5.5f), 207f, 126f, false, line)
                face == "love" -> { fill.color = Color.rgb(230, 64, 102); heart(canvas, x, eyeY, 4.2f); fill.color = ink }
                face == "wink" && x > 0 ->
                    canvas.drawArc(RectF(x - 3.5f, eyeY - 1.5f, x + 3.5f, eyeY + 5.5f), 207f, 126f, false, line)
                face == "alarm" || face == "surprised" -> {
                    fill.color = Color.WHITE
                    canvas.drawCircle(x, eyeY, 5f, fill)
                    fill.color = ink
                    canvas.drawCircle(x, eyeY + .5f, 2.6f, fill)
                }
                else -> {
                    canvas.drawOval(RectF(x - 3.2f, eyeY - 4.2f, x + 3.2f, eyeY + 4.2f), fill)
                    fill.color = Color.WHITE
                    canvas.drawCircle(x + 1.1f, eyeY - 1.8f, 1.1f, fill)
                    fill.color = ink
                }
            }
        }
        // brows: inner ends raised when worried
        line.strokeWidth = 1.6f
        if (face == "busy" || face == "alarm") {
            val lift = if (face == "busy") .8f else 1.6f
            canvas.drawLine(-13f, eyeY - 8 + lift, -5f, eyeY - 8 - lift, line)
            canvas.drawLine(5f, eyeY - 8 - lift, 13f, eyeY - 8 + lift, line)
        }
        // mouth
        val mouthY = 7f
        line.strokeWidth = 2f
        when (face) {
            "happy", "hop", "wink", "love" -> canvas.drawArc(RectF(-6f, mouthY - 9, 6f, mouthY + 3), 36f, 108f, false, line)
            "cheer" -> {
                val path = Path().apply {
                    moveTo(-7f, mouthY - 1); cubicTo(-6f, mouthY + 8, 6f, mouthY + 8, 7f, mouthY - 1); close()
                }
                canvas.drawPath(path, fill)
                fill.color = Color.rgb(255, 140, 153)
                canvas.drawArc(RectF(-2.6f, mouthY + .9f, 2.6f, mouthY + 6.1f), 0f, 180f, true, fill)
                fill.color = ink
            }
            "busy" -> canvas.drawPath(Path().apply {
                moveTo(-5f, mouthY + 1); cubicTo(-2f, mouthY - 1, 2f, mouthY + 3, 5f, mouthY + 1)
            }, line)
            "alarm", "surprised" -> canvas.drawOval(RectF(-2.6f, mouthY - 2.4f, 2.6f, mouthY + 4.4f), fill)
            "yawn" -> canvas.drawOval(RectF(-4.2f, mouthY - 3.9f, 4.2f, mouthY + 7.9f), fill)
            else -> canvas.drawCircle(0f, mouthY + 1, 1.8f, fill)
        }
        if (face in setOf("happy", "cheer", "sleep", "love", "wink", "hop")) {      // cheeks
            fill.color = Color.argb(if (face == "love") 115 else 90, 255, 115, 128)
            canvas.drawCircle(-15f, 3f, 3.5f, fill)
            canvas.drawCircle(15f, 3f, 3.5f, fill)
        }
        if (face == "cheer") {                                                      // hands up
            fill.color = rgb(r * .8f, g * .8f, b * .8f)
            canvas.drawCircle(-26f, -14f, 4.5f, fill)
            canvas.drawCircle(26f, -14f, 4.5f, fill)
        }
    }

    private fun heart(canvas: Canvas, x: Float, y: Float, size: Float) {
        canvas.drawPath(Path().apply {
            moveTo(x, y + size * .35f)
            cubicTo(x - size, y - size * .4f, x - size * .35f, y - size, x, y - size * .35f)
            cubicTo(x + size * .35f, y - size, x + size, y - size * .4f, x, y + size * .35f)
            close()
        }, fill)
    }

    private fun drawExtras(canvas: Canvas, face: String, cx: Float, cy: Float) {
        when (face) {
            "sleep" -> {
                text.color = Color.argb(230, 102, 122, 230)
                for (i in 0..1) {
                    val t = (phase * .5f + i * .5f) % 1f
                    text.textSize = 8f + 4 * t
                    text.alpha = ((1 - t) * 230).toInt()
                    canvas.drawText("z", cx + 22 + t * 8, cy - 20 - t * 14, text)
                }
            }
            "alarm" -> {
                text.color = Color.rgb(220, 50, 47)
                text.alpha = 255
                text.textSize = 16f
                canvas.drawText("!", cx - 31, cy - 14, text)
                fill.color = Color.rgb(140, 200, 255)                           // sweat drop
                canvas.drawCircle(cx + 22, cy - 12 + (phase * 6) % 6, 2.4f, fill)
            }
            "love", "cheer" -> {
                fill.color = Color.rgb(240, 90, 120)
                val t = (phase * .4f) % 1f
                heart(canvas, cx + 24, cy - 22 - t * 10, 4f)
            }
        }
    }

    private fun drawBadge(canvas: Canvas) {
        fill.color = Color.rgb(220, 50, 47)
        canvas.drawCircle(68f, 18f, 9f, fill)
        text.color = Color.WHITE
        text.alpha = 255
        text.textSize = 11f
        canvas.drawText(if (pressing > 9) "9+" else pressing.toString(), 68f, 22f, text)
    }
}
