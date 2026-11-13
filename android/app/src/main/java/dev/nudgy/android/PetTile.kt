package dev.nudgy.android

import android.annotation.SuppressLint
import android.app.PendingIntent
import android.content.Intent
import android.os.Build
import android.provider.Settings
import android.service.quicksettings.Tile
import android.service.quicksettings.TileService

/** Quick Settings tile: shows or hides the pet from anywhere, like Super+T on the laptop. */
class PetTile : TileService() {
    override fun onStartListening() = update()

    override fun onClick() {
        val prefs = Prefs(this)
        if (prefs.running) {
            PetService.stop(this)
            prefs.running = false
        } else if (Settings.canDrawOverlays(this)) {
            // Some Android versions don't let a tile start it from the background; open the app instead.
            runCatching { PetService.start(this); prefs.running = true }.onFailure { openApp() }
        } else {
            openApp()
        }
        update()
    }

    @SuppressLint("StartActivityAndCollapseDeprecated")   // only used below Android 14
    @Suppress("DEPRECATION")
    private fun openApp() {
        val intent = Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        if (Build.VERSION.SDK_INT >= 34) {
            startActivityAndCollapse(PendingIntent.getActivity(this, 0, intent, PendingIntent.FLAG_IMMUTABLE))
        } else {
            startActivityAndCollapse(intent)
        }
    }

    private fun update() {
        qsTile?.apply {
            state = if (Prefs(this@PetTile).running) Tile.STATE_ACTIVE else Tile.STATE_INACTIVE
            updateTile()
        }
    }
}
