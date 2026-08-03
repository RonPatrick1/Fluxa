package com.liamapp.bose_battery_voice

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Build

class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED &&
            intent.action != Intent.ACTION_MY_PACKAGE_REPLACED
        ) return
        val enabled = BatteryVoiceSettings.preferences(context)
            .getBoolean(BatteryVoiceSettings.KEY_MONITORING, false)
        if (!enabled) return

        val service = Intent(context, BoseMonitoringService::class.java)
            .setAction(BoseMonitoringService.ACTION_START)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            context.startForegroundService(service)
        } else {
            context.startService(service)
        }
    }
}
