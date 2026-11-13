package dev.nudgy.android

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.provider.Settings

/** Brings the pet back after a restart if it was showing. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED && Prefs(context).running &&
            Settings.canDrawOverlays(context)) {
            PetService.start(context)
        }
    }
}
