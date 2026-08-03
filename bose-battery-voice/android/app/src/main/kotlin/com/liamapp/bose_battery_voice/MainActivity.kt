package com.liamapp.bose_battery_voice

import android.Manifest
import android.content.Intent
import android.content.ActivityNotFoundException
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.PowerManager
import android.provider.Settings
import androidx.annotation.NonNull
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodCall
import io.flutter.plugin.common.MethodChannel

class MainActivity : FlutterActivity() {
    companion object {
        private const val CHANNEL = "com.liamapp.bose_battery_voice/control"
        private const val PERMISSION_REQUEST = 4172
        private const val NOTIFICATION_PERMISSION_REQUEST = 4173
    }

    private var pendingPermissionResult: MethodChannel.Result? = null
    private var pendingNotificationResult: MethodChannel.Result? = null

    override fun configureFlutterEngine(@NonNull flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, CHANNEL)
            .setMethodCallHandler(::handleMethod)
    }

    override fun onStart() {
        super.onStart()
        val monitoring = BatteryVoiceSettings.preferences(this)
            .getBoolean(BatteryVoiceSettings.KEY_MONITORING, false)
        if (monitoring && hasRequiredPermissions()) {
            val skipExisting = intent.getBooleanExtra(
                BoseMonitoringService.EXTRA_SKIP_ALREADY_CONNECTED,
                false,
            )
            startMonitor(skipExisting)
            intent.removeExtra(BoseMonitoringService.EXTRA_SKIP_ALREADY_CONNECTED)
        }
    }

    private fun handleMethod(call: MethodCall, result: MethodChannel.Result) {
        when (call.method) {
            "getStatus" -> result.success(status())
            "requestPermissions" -> requestBluetoothPermissions(result)
            "setStatusNotifications" -> setStatusNotifications(call, result)
            "requestUnrestrictedBattery" -> {
                result.success(requestUnrestrictedBattery())
            }
            "openSamsungBackgroundSettings" -> {
                result.success(openSamsungBackgroundSettings())
            }
            "openNotificationSettings" -> {
                openNotificationSettings()
                result.success(null)
            }
            "setSpeechSettings" -> setSpeechSettings(call, result)
            "setMonitoring" -> {
                val enabled = call.argument<Boolean>("enabled") == true
                if (enabled && !hasRequiredPermissions()) {
                    result.error("permission", "Bluetooth permission is required.", null)
                    return
                }
                BatteryVoiceSettings.preferences(this).edit()
                    .putBoolean(BatteryVoiceSettings.KEY_MONITORING, enabled)
                    .apply()
                if (enabled) startMonitor() else stopMonitor()
                result.success(null)
            }
            "setSpeakerEnabled" -> {
                val id = call.argument<String>("id")
                val enabled = call.argument<Boolean>("enabled") == true
                val key = BatteryVoiceSettings.enabledKey(id)
                if (key == null) {
                    result.error("speaker", "Unknown speaker.", null)
                    return
                }
                BatteryVoiceSettings.preferences(this).edit().putBoolean(key, enabled).apply()
                result.success(null)
            }
            "announceNow" -> {
                if (!hasRequiredPermissions()) {
                    result.error("permission", "Bluetooth permission is required.", null)
                    return
                }
                val id = call.argument<String>("id")
                if (BatteryVoiceSettings.speaker(id) == null) {
                    result.error("speaker", "Unknown speaker.", null)
                    return
                }
                val intent = Intent(this, BoseMonitoringService::class.java)
                    .setAction(BoseMonitoringService.ACTION_ANNOUNCE)
                    .putExtra(BoseMonitoringService.EXTRA_SPEAKER_ID, id)
                startForegroundServiceCompat(intent)
                result.success(null)
            }
            "testAnnouncement" -> {
                if (!hasRequiredPermissions()) {
                    result.error("permission", "Bluetooth permission is required.", null)
                    return
                }
                val intent = Intent(this, BoseMonitoringService::class.java)
                    .setAction(BoseMonitoringService.ACTION_TEST_ACTIVE)
                startForegroundServiceCompat(intent)
                result.success(null)
            }
            else -> result.notImplemented()
        }
    }

    private fun status(): Map<String, Any> {
        val preferences = BatteryVoiceSettings.preferences(this)
        return mapOf(
            "platform" to "Android",
            "monitoring" to preferences.getBoolean(BatteryVoiceSettings.KEY_MONITORING, false),
            "permissionGranted" to hasRequiredPermissions(),
            "showStatusNotifications" to preferences.getBoolean(
                BatteryVoiceSettings.KEY_SHOW_STATUS_NOTIFICATIONS,
                false,
            ),
            "notificationPermissionGranted" to hasNotificationPermission(),
            "batteryOptimizationIgnored" to isBatteryOptimizationIgnored(),
            "isSamsung" to Build.MANUFACTURER.equals("samsung", ignoreCase = true),
            "speechTemplate" to BatteryVoiceSettings.speechTemplate(this),
            "deviceLabel" to BatteryVoiceSettings.deviceLabel(this),
            "announcementVolumePercent" to BatteryVoiceSettings.announcementVolumePercent(this),
            "elizabethEnabled" to preferences.getBoolean(
                BatteryVoiceSettings.KEY_ELIZABETH_ENABLED,
                true,
            ),
            "freddieEnabled" to preferences.getBoolean(
                BatteryVoiceSettings.KEY_FREDDIE_ENABLED,
                false,
            ),
            "lastEvent" to preferences.getString(BatteryVoiceSettings.KEY_LAST_EVENT, "").orEmpty(),
        )
    }

    private fun requestBluetoothPermissions(result: MethodChannel.Result) {
        if (hasRequiredPermissions()) {
            result.success(true)
            return
        }
        if (pendingPermissionResult != null) {
            result.error("permission_pending", "A permission request is already open.", null)
            return
        }
        pendingPermissionResult = result
        requestPermissions(requiredPermissions(), PERMISSION_REQUEST)
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray,
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == PERMISSION_REQUEST) {
            pendingPermissionResult?.success(hasRequiredPermissions())
            pendingPermissionResult = null
        } else if (requestCode == NOTIFICATION_PERMISSION_REQUEST) {
            val granted = hasNotificationPermission()
            if (!granted) {
                BatteryVoiceSettings.preferences(this).edit()
                    .putBoolean(BatteryVoiceSettings.KEY_SHOW_STATUS_NOTIFICATIONS, false)
                    .apply()
            }
            pendingNotificationResult?.success(granted)
            pendingNotificationResult = null
        }
    }

    private fun requiredPermissions(): Array<String> {
        val permissions = mutableListOf<String>()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            permissions += Manifest.permission.BLUETOOTH_CONNECT
        }
        return permissions.toTypedArray()
    }

    private fun hasRequiredPermissions(): Boolean = requiredPermissions().all {
        checkSelfPermission(it) == PackageManager.PERMISSION_GRANTED
    }

    private fun hasNotificationPermission(): Boolean =
        Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU ||
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) ==
            PackageManager.PERMISSION_GRANTED

    private fun setStatusNotifications(call: MethodCall, result: MethodChannel.Result) {
        val enabled = call.argument<Boolean>("enabled")
        if (enabled == null) {
            result.error("notification", "Invalid notification setting.", null)
            return
        }
        BatteryVoiceSettings.preferences(this).edit()
            .putBoolean(BatteryVoiceSettings.KEY_SHOW_STATUS_NOTIFICATIONS, enabled)
            .apply()
        if (!enabled || hasNotificationPermission()) {
            // Android does not let an app revoke its own notification permission.
            // Return whether the requested drawer visibility is already effective.
            result.success(enabled || !hasNotificationPermission())
            return
        }
        if (pendingNotificationResult != null) {
            result.error("permission_pending", "A permission request is already open.", null)
            return
        }
        pendingNotificationResult = result
        requestPermissions(
            arrayOf(Manifest.permission.POST_NOTIFICATIONS),
            NOTIFICATION_PERMISSION_REQUEST,
        )
    }

    private fun setSpeechSettings(call: MethodCall, result: MethodChannel.Result) {
        val template = call.argument<String>("template")?.trim().orEmpty()
        val deviceLabel = call.argument<String>("deviceLabel")?.trim().orEmpty()
        val announcementVolumePercent = call.argument<Int>("announcementVolumePercent")
            ?: BatteryVoiceSettings.announcementVolumePercent(this)
        if (template.isEmpty() || template.length > 240) {
            result.error("speech", "The announcement must be 1 to 240 characters.", null)
            return
        }
        if (deviceLabel.isEmpty() || deviceLabel.length > 80) {
            result.error("speech", "The device name must be 1 to 80 characters.", null)
            return
        }
        if (announcementVolumePercent !in 20..75) {
            result.error("speech", "Announcement volume must be between 20 and 75%.", null)
            return
        }
        BatteryVoiceSettings.preferences(this).edit()
            .putString(BatteryVoiceSettings.KEY_SPEECH_TEMPLATE, template)
            .putString(BatteryVoiceSettings.KEY_DEVICE_LABEL, deviceLabel)
            .putInt(
                BatteryVoiceSettings.KEY_ANNOUNCEMENT_VOLUME_PERCENT,
                announcementVolumePercent,
            )
            .apply()
        result.success(null)
    }

    private fun openNotificationSettings() {
        val intent = Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS)
            .putExtra(Settings.EXTRA_APP_PACKAGE, packageName)
            .setData(Uri.parse("package:$packageName"))
        startActivity(intent)
    }

    private fun isBatteryOptimizationIgnored(): Boolean {
        val powerManager = getSystemService(POWER_SERVICE) as PowerManager
        return powerManager.isIgnoringBatteryOptimizations(packageName)
    }

    private fun requestUnrestrictedBattery(): Boolean {
        if (isBatteryOptimizationIgnored()) return true
        val candidates = listOf(
            Intent(
            Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
            Uri.parse("package:$packageName"),
            ),
            Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS),
            Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS)
                .setData(Uri.parse("package:$packageName")),
        )
        return startFirstAvailable(candidates)
    }

    private fun openSamsungBackgroundSettings(): Boolean {
        val candidates = listOf(
            Intent("com.samsung.android.sm.ACTION_BATTERY")
                .setPackage("com.samsung.android.lool"),
            Intent(Settings.ACTION_BATTERY_SAVER_SETTINGS),
            Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS),
            Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS)
                .setData(Uri.parse("package:$packageName")),
        )
        return startFirstAvailable(candidates)
    }

    private fun startFirstAvailable(candidates: List<Intent>): Boolean {
        for (intent in candidates) {
            if (intent.resolveActivity(packageManager) == null) continue
            try {
                startActivity(intent)
                return true
            } catch (_: ActivityNotFoundException) {
            } catch (_: SecurityException) {
                // Some Samsung builds resolve private Device Care activities
                // but then reject third-party launches. Continue to Android's
                // public battery or per-app settings instead.
            }
        }
        return false
    }

    private fun startMonitor(skipAlreadyConnected: Boolean = false) {
        startForegroundServiceCompat(
            Intent(this, BoseMonitoringService::class.java)
                .setAction(BoseMonitoringService.ACTION_START)
                .putExtra(
                    BoseMonitoringService.EXTRA_SKIP_ALREADY_CONNECTED,
                    skipAlreadyConnected,
                ),
        )
    }

    private fun stopMonitor() {
        stopService(Intent(this, BoseMonitoringService::class.java))
    }

    private fun startForegroundServiceCompat(intent: Intent) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(intent)
        } else {
            startService(intent)
        }
    }
}
