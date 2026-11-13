package dev.nudgy.android

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.view.Gravity
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView

/** Setup: pick the shared folder, allow floating, then show or hide the pet. */
class MainActivity : Activity() {
    private lateinit var prefs: Prefs
    private lateinit var steps: LinearLayout

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = Prefs(this)
        steps = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(48, 96, 48, 48)
        }
        setContentView(ScrollView(this).apply { addView(steps) })
        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 2)
        }
    }

    override fun onResume() {
        super.onResume()
        render()
    }

    private fun render() {
        steps.removeAllViews()
        val pet = PetView(this).apply { pressing = 0 }
        steps.addView(pet, LinearLayout.LayoutParams(260, 260).apply { gravity = Gravity.CENTER_HORIZONTAL })
        text("Nudgy", 26f, bold = true)
        text("A little pet that floats over your apps and shows what needs you now, as your laptop's " +
            "Nudgy sees it. It reads one file, nudgy-now.json, from a folder your laptop shares with this phone " +
            "(for example with Syncthing). It doesn't use the internet.", 14f)

        val folder = prefs.folder
        val found = folder?.let { Snapshot.read(this, it) }
        step("1. Shared folder",
            when {
                folder == null -> "Pick the folder that receives nudgy-now.json from your laptop.\n" +
                    "On the laptop: nudgy phone ~/Sync/nudgy"
                found == null -> "Picked, but nudgy-now.json isn't there yet. Check that the laptop is sharing it."
                else -> "Connected. ${found.pressing} need you now."
            },
            if (folder == null) "Pick folder" else "Change folder") {
            startActivityForResult(Intent(Intent.ACTION_OPEN_DOCUMENT_TREE), PICK)
        }
        val canFloat = Settings.canDrawOverlays(this)
        step("2. Float over other apps",
            if (canFloat) "Allowed." else "Android asks you to allow this once, for Nudgy only.",
            if (canFloat) null else "Allow") {
            startActivity(Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:$packageName")))
        }
        step("3. Keep it running",
            "Vivo and some other phones stop background apps. In Settings > Battery, set Nudgy to " +
                "'Unrestricted' or allow background activity, or the pet will disappear now and then.",
            "Battery settings") {
            startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
        }
        step("4. Show the pet",
            "Tap the pet for your list, drag it anywhere. You can also add the Nudgy tile to Quick Settings " +
                "(pull down twice, edit) to show or hide it from anywhere.",
            if (prefs.running) "Hide Nudgy" else "Show Nudgy", enabled = canFloat) {
            if (prefs.running) {
                PetService.stop(this)
                prefs.running = false
            } else {
                PetService.start(this)
            }
            steps.postDelayed({ render() }, 300)
        }
    }

    private fun text(value: String, size: Float, bold: Boolean = false) = steps.addView(TextView(this).apply {
        text = value
        textSize = size
        setTextColor(if (bold) Color.rgb(32, 33, 36) else Color.rgb(80, 84, 90))
        paint.isFakeBoldText = bold
        setPadding(0, 12, 0, 12)
    })

    private fun step(title: String, body: String, button: String?, enabled: Boolean = true, action: () -> Unit) {
        text(title, 17f, bold = true)
        text(body, 14f)
        if (button != null) {
            steps.addView(Button(this).apply {
                text = button
                isEnabled = enabled
                setOnClickListener { action() }
            })
        }
    }

    @Deprecated("Activity result API needs AndroidX; this keeps the app free of extra libraries.")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        val tree = data?.data
        if (requestCode == PICK && resultCode == RESULT_OK && tree != null) {
            contentResolver.takePersistableUriPermission(tree, Intent.FLAG_GRANT_READ_URI_PERMISSION)
            prefs.folder = tree
            if (prefs.running) {
                startService(Intent(this, PetService::class.java).setAction(PetService.ACTION_REFRESH))
            }
            render()
        }
    }

    companion object {
        const val PICK = 1
    }
}
