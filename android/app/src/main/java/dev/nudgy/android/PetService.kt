package dev.nudgy.android

import android.animation.ValueAnimator
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.ServiceInfo
import android.graphics.Color
import android.graphics.PixelFormat
import android.graphics.drawable.GradientDrawable
import android.net.Uri
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.provider.Settings
import android.util.TypedValue
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.ViewConfiguration
import android.view.WindowManager
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import java.time.format.DateTimeFormatter
import kotlin.math.abs

/**
 * Keeps the pet floating over other apps. Tap it for what needs you; drag it anywhere (it snaps to
 * the nearest side). It re-reads the laptop's file every minute while the screen is on.
 */
class PetService : Service() {
    private lateinit var windows: WindowManager
    private lateinit var prefs: Prefs
    private lateinit var pet: PetView
    private lateinit var petParams: WindowManager.LayoutParams
    private var panel: View? = null
    private var bubble: View? = null
    private var snapshot = Snapshot.EMPTY
    private val main = Handler(Looper.getMainLooper())
    private var screenOn = true

    private val frame = object : Runnable {
        override fun run() {
            pet.tick()
            if (screenOn) main.postDelayed(this, FRAME_MS)
        }
    }
    private val reload = object : Runnable {
        override fun run() {
            refresh()
            // Screen off: still look every 10 minutes, so nudges arrive in your pocket too.
            main.postDelayed(this, if (screenOn) RELOAD_MS else RELOAD_OFF_MS)
        }
    }
    private val idleLife = object : Runnable {
        override fun run() {
            if (pet.face() == "happy") pet.act(listOf("wink", "hop", "yawn").random(), 1200)
            main.postDelayed(this, (25_000L..60_000L).random())
        }
    }
    private val screen = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            screenOn = intent.action == Intent.ACTION_SCREEN_ON
            main.removeCallbacks(frame)
            main.removeCallbacks(reload)
            if (screenOn) main.post(frame)
            main.post(reload)
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        prefs = Prefs(this)
        windows = getSystemService(WINDOW_SERVICE) as WindowManager
        startInForeground()
        if (!Settings.canDrawOverlays(this)) {
            stopSelf()
            return
        }
        prefs.running = true
        addPet()
        registerReceiver(screen, IntentFilter().apply {
            addAction(Intent.ACTION_SCREEN_ON)
            addAction(Intent.ACTION_SCREEN_OFF)
        })
        main.post(frame)
        main.post(reload)
        main.postDelayed(idleLife, 30_000)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                prefs.running = false
                stopSelf()
            }
            ACTION_PANEL -> togglePanel()
            ACTION_REFRESH -> refresh()
        }
        return START_STICKY
    }

    override fun onDestroy() {
        main.removeCallbacksAndMessages(null)
        runCatching { unregisterReceiver(screen) }
        closePanel()
        closeBubble()
        if (::pet.isInitialized && pet.isAttachedToWindow) windows.removeView(pet)
        super.onDestroy()
    }

    // ---- the notification Android requires for anything that stays on screen ------------------------------
    private fun startInForeground() {
        val manager = getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL, "Floating pet", NotificationManager.IMPORTANCE_MIN).apply {
                description = "Shown while Nudgy floats on your screen"
                setShowBadge(false)
            })
        manager.createNotificationChannel(
            NotificationChannel(NUDGES, "Nudges", NotificationManager.IMPORTANCE_DEFAULT).apply {
                description = "When something newly needs you, and reminders for overdue things"
            })
        val notification = notification()
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(1, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(1, notification)
        }
    }

    private fun notification(): Notification {
        val open = PendingIntent.getService(this, 1, Intent(this, PetService::class.java).setAction(ACTION_PANEL),
            PendingIntent.FLAG_IMMUTABLE)
        val stop = PendingIntent.getService(this, 2, Intent(this, PetService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE)
        val text = when {
            snapshot.pressing > 0 -> "${snapshot.pressing} need you now · ${snapshot.now.firstOrNull()?.title ?: ""}"
            else -> "All calm"
        }
        return Notification.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_nudgy)
            .setContentTitle("Nudgy")
            .setContentText(text)
            .setContentIntent(open)
            .setOngoing(true)
            .addAction(Notification.Action.Builder(null, "Hide", stop).build())
            .build()
    }

    // ---- the pet ------------------------------------------------------------------------------------------------
    private fun dp(value: Float): Int =
        TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, value, resources.displayMetrics).toInt()

    private fun overlayParams(width: Int, height: Int, flags: Int = 0) = WindowManager.LayoutParams(
        width, height, WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
        WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS or flags,
        PixelFormat.TRANSLUCENT).apply { gravity = Gravity.TOP or Gravity.START }

    private fun screenWidth() = resources.displayMetrics.widthPixels
    private fun screenHeight() = resources.displayMetrics.heightPixels

    private fun addPet() {
        pet = PetView(this)
        val size = dp(PET_DP)
        petParams = overlayParams(size, size).apply {
            x = if (prefs.x < 0) screenWidth() - size else prefs.x
            y = prefs.y.coerceIn(0, screenHeight() - size)
        }
        val slop = ViewConfiguration.get(this).scaledTouchSlop
        var startX = 0f
        var startY = 0f
        var originX = 0
        var originY = 0
        var dragging = false
        pet.setOnTouchListener { _, event ->
            when (event.actionMasked) {
                MotionEvent.ACTION_DOWN -> {
                    startX = event.rawX; startY = event.rawY
                    originX = petParams.x; originY = petParams.y
                    dragging = false
                }
                MotionEvent.ACTION_MOVE -> {
                    val dx = event.rawX - startX
                    val dy = event.rawY - startY
                    if (dragging || abs(dx) > slop || abs(dy) > slop) {
                        dragging = true
                        closePanel()
                        closeBubble()
                        petParams.x = (originX + dx).toInt()
                        petParams.y = (originY + dy).toInt().coerceIn(0, screenHeight() - size)
                        windows.updateViewLayout(pet, petParams)
                    }
                }
                MotionEvent.ACTION_UP -> if (dragging) snapToSide() else pet.performClick()
            }
            true
        }
        pet.setOnClickListener { togglePanel() }
        pet.contentDescription = "Nudgy. Tap for what needs you now."
        windows.addView(pet, petParams)
    }

    private fun snapToSide() {
        val size = dp(PET_DP)
        val target = if (petParams.x + size / 2 < screenWidth() / 2) 0 else screenWidth() - size
        ValueAnimator.ofInt(petParams.x, target).apply {
            duration = 220
            addUpdateListener {
                petParams.x = it.animatedValue as Int
                if (pet.isAttachedToWindow) windows.updateViewLayout(pet, petParams)
            }
            start()
        }
        prefs.x = target
        prefs.y = petParams.y
    }

    // ---- reading the laptop's file ------------------------------------------------------------------------------
    private fun refresh() {
        val folder = prefs.folder ?: return
        Thread {
            val fresh = Snapshot.read(this, folder)
            main.post { if (fresh != null) apply(fresh) else markStale() }
        }.start()
    }

    private fun markStale() {
        pet.stale = true
    }

    private fun apply(fresh: Snapshot) {
        val before = snapshot
        snapshot = fresh
        pet.pressing = fresh.pressing
        pet.alarm = fresh.alarm
        pet.stale = fresh.stale()
        if (fresh.pressing > before.pressing && before !== Snapshot.EMPTY) pet.act("surprised", 1200)
        getSystemService(NotificationManager::class.java).notify(1, notification())
        announce(fresh)
        if (panel != null) {
            closePanel()
            openPanel()
        }
    }

    /** A bubble for something that newly needs you. */
    private fun announce(fresh: Snapshot) {
        val told = prefs.told
        val firstRun = told.isEmpty()
        val keys = fresh.now.map { "now:" + it.id }.toSet()
        prefs.told = (told.filter { it in keys } + keys).toSet().ifEmpty { setOf("-") }
        if (firstRun) {                              // don't announce everything the first time
            prefs.lastReminder = System.currentTimeMillis()
            return
        }
        val urgent = fresh.now.firstOrNull { "now:" + it.id !in told }
        if (urgent != null) {
            val text = urgent.title + if (urgent.whenText.isNotEmpty()) " (${urgent.whenText})" else ""
            pet.act("surprised", 1200)
            if (screenOn) showBubble("Needs you now:\n$text", urgent.url)
            nudge("Needs you now", text, urgent.url)
        } else if (fresh.alarm) {
            remindOverdue(fresh)
        }
    }

    /** Overdue things get a gentle reminder every couple of hours, during the day only. */
    private fun remindOverdue(fresh: Snapshot) {
        val now = System.currentTimeMillis()
        val hour = java.time.LocalTime.now().hour
        if (hour !in 8..21 || now - prefs.lastReminder < REMIND_MS) return
        val first = fresh.now.firstOrNull() ?: return
        prefs.lastReminder = now
        nudge("Still waiting for you", first.title + if (first.whenText.isNotEmpty()) " (${first.whenText})" else "",
            first.url)
    }

    /** A normal notification, so the nudge reaches you even when the screen is off or the pet is hidden. */
    private fun nudge(title: String, text: String, url: String) {
        val manager = getSystemService(NotificationManager::class.java)
        val tap = if (url.isNotEmpty()) {
            PendingIntent.getActivity(this, 3, Intent(Intent.ACTION_VIEW, Uri.parse(url)), PendingIntent.FLAG_IMMUTABLE)
        } else {
            PendingIntent.getService(this, 4, Intent(this, PetService::class.java).setAction(ACTION_PANEL),
                PendingIntent.FLAG_IMMUTABLE)
        }
        prefs.lastReminder = System.currentTimeMillis()          // any nudge restarts the reminder clock
        manager.notify(NUDGE_ID, Notification.Builder(this, NUDGES)
            .setSmallIcon(R.drawable.ic_nudgy)
            .setContentTitle(title)
            .setContentText(text)
            .setStyle(Notification.BigTextStyle().bigText(text))
            .setContentIntent(tap)
            .setAutoCancel(true)
            .build())
    }

    // ---- speech bubble ----------------------------------------------------------------------------------------------
    private fun card(): GradientDrawable = GradientDrawable().apply {
        setColor(Color.WHITE)
        cornerRadius = dp(14f).toFloat()
        setStroke(dp(1f), Color.rgb(220, 224, 228))
    }

    private fun showBubble(message: String, url: String) {
        closeBubble()
        val label = TextView(this).apply {
            text = message
            setTextColor(Color.rgb(32, 33, 36))
            textSize = 13f
            maxWidth = dp(230f)
            setPadding(dp(12f), dp(9f), dp(12f), dp(9f))
            background = card()
            elevation = dp(4f).toFloat()
            setOnClickListener {
                closeBubble()
                if (url.isNotEmpty()) open(url)
            }
        }
        val onLeft = petParams.x < screenWidth() / 2
        val params = overlayParams(WindowManager.LayoutParams.WRAP_CONTENT, WindowManager.LayoutParams.WRAP_CONTENT).apply {
            gravity = Gravity.TOP or if (onLeft) Gravity.START else Gravity.END
            x = dp(PET_DP + 4f)
            y = petParams.y
        }
        windows.addView(label, params)
        bubble = label
        main.postDelayed({ closeBubble() }, BUBBLE_MS)
    }

    private fun closeBubble() {
        bubble?.let { if (it.isAttachedToWindow) windows.removeView(it) }
        bubble = null
    }

    // ---- the list ----------------------------------------------------------------------------------------------
    private fun togglePanel() = if (panel != null) closePanel() else openPanel()

    private fun closePanel() {
        panel?.let { if (it.isAttachedToWindow) windows.removeView(it) }
        panel = null
    }

    private fun openPanel() {
        closeBubble()
        refresh()
        val list = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(14f), dp(12f), dp(14f), dp(12f))
        }
        fun heading(text: String) = list.addView(TextView(this).apply {
            this.text = text
            textSize = 13f
            setTextColor(Color.rgb(95, 99, 104))
            setPadding(0, dp(8f), 0, dp(2f))
            paint.isFakeBoldText = true
        })
        fun row(entry: Entry) = list.addView(TextView(this).apply {
            text = "• " + entry.title + if (entry.whenText.isNotEmpty()) "  · ${entry.whenText}" else ""
            textSize = 14f
            maxLines = 2
            ellipsize = android.text.TextUtils.TruncateAt.END
            setTextColor(Color.rgb(32, 33, 36))
            setPadding(0, dp(5f), 0, dp(5f))
            if (entry.url.isNotEmpty()) setOnClickListener { closePanel(); open(entry.url) }
        })
        when {
            prefs.folder == null -> heading("Open the Nudgy app and pick the folder your laptop shares.")
            snapshot === Snapshot.EMPTY -> heading("No nudgy-now.json in that folder yet. Is the laptop sharing it?")
            else -> {
                if (snapshot.newer) heading("Your laptop's Nudgy is newer than this app: please update the app.")
                heading(if (snapshot.now.isEmpty()) "Nothing urgent. 🌿" else "Needs you now (${snapshot.pressing})")
                snapshot.now.forEach(::row)
                if (snapshot.soon.isNotEmpty()) heading("Soon")
                snapshot.soon.forEach(::row)
            }
        }
        val updated = snapshot.updated?.let {
            "Laptop updated " + it.atZoneSameInstant(java.time.ZoneId.systemDefault())
                .format(DateTimeFormatter.ofPattern("EEE HH:mm"))
        } ?: ""
        list.addView(TextView(this).apply {
            text = updated + if (snapshot.stale() && snapshot !== Snapshot.EMPTY) " · may be out of date" else ""
            textSize = 11f
            setTextColor(Color.rgb(128, 134, 139))
            setPadding(0, dp(10f), 0, 0)
        })
        val scroll = ScrollView(this).apply {
            addView(list)
            background = card()
            elevation = dp(6f).toFloat()
            setOnTouchListener { _, event ->
                if (event.actionMasked == MotionEvent.ACTION_OUTSIDE) closePanel()
                false
            }
        }
        val width = (screenWidth() * .82f).toInt().coerceAtMost(dp(340f))
        val below = petParams.y + dp(PET_DP) + dp(4f)
        val params = overlayParams(width, WindowManager.LayoutParams.WRAP_CONTENT,
            WindowManager.LayoutParams.FLAG_WATCH_OUTSIDE_TOUCH).apply {
            x = (petParams.x + dp(PET_DP) / 2 - width / 2).coerceIn(dp(8f), screenWidth() - width - dp(8f))
            y = if (below < screenHeight() * .6f) below else dp(40f)
        }
        windows.addView(scroll, params)
        panel = scroll
        scroll.post {                               // never taller than most of the screen
            val limit = (screenHeight() * .62f).toInt()
            if (scroll.height > limit && scroll.isAttachedToWindow) {
                params.height = limit
                windows.updateViewLayout(scroll, params)
            }
        }
    }

    private fun open(url: String) {
        runCatching {
            startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        }
    }

    companion object {
        const val CHANNEL = "pet"
        const val ACTION_STOP = "dev.nudgy.android.STOP"
        const val ACTION_PANEL = "dev.nudgy.android.PANEL"
        const val ACTION_REFRESH = "dev.nudgy.android.REFRESH"
        const val PET_DP = 64f
        const val FRAME_MS = 100L
        const val RELOAD_MS = 60_000L
        const val RELOAD_OFF_MS = 10 * 60_000L
        const val REMIND_MS = 2 * 60 * 60_000L
        const val NUDGES = "nudges"
        const val NUDGE_ID = 7
        const val BUBBLE_MS = 30_000L

        fun start(context: Context) {
            context.startForegroundService(Intent(context, PetService::class.java))
        }

        fun stop(context: Context) {
            context.startService(Intent(context, PetService::class.java).setAction(ACTION_STOP))
        }
    }
}
