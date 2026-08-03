package com.liamapp.bose_battery_voice

import android.content.Context
import android.content.SharedPreferences
import java.util.Locale

data class FamilySpeaker(
    val id: String,
    val name: String,
    val address: String,
    val enabledKey: String,
    val enabledByDefault: Boolean,
)

object BatteryVoiceSettings {
    const val KEY_MONITORING = "monitoring"
    const val KEY_ELIZABETH_ENABLED = "elizabeth_enabled"
    const val KEY_FREDDIE_ENABLED = "freddie_enabled"
    const val KEY_SHOW_STATUS_NOTIFICATIONS = "show_status_notifications"
    const val KEY_SPEECH_TEMPLATE = "speech_template"
    const val KEY_DEVICE_LABEL = "device_label"
    const val KEY_ANNOUNCEMENT_VOLUME_PERCENT = "announcement_volume_percent"
    const val KEY_SUPPRESS_UNTIL_DISCONNECT = "suppress_until_disconnect"
    const val KEY_LAST_EVENT = "last_event"
    const val KEY_LAST_ANNOUNCEMENT_PREFIX = "last_announcement_"
    const val LEGACY_SPEECH_TEMPLATE = "Battery {battery} percent."
    const val DEFAULT_SPEECH_TEMPLATE =
        "{devices} connected to {speaker}. Battery {battery} percent."
    const val DEFAULT_ANNOUNCEMENT_VOLUME_PERCENT = 45

    val ELIZABETH = FamilySpeaker(
        id = "elizabeth",
        name = "Elizabeth's Bose",
        address = "BC:87:FA:2E:7C:63",
        enabledKey = KEY_ELIZABETH_ENABLED,
        enabledByDefault = true,
    )
    val FREDDIE = FamilySpeaker(
        id = "freddie",
        name = "Freddie's Bose",
        address = "68:F2:1F:93:49:0A",
        enabledKey = KEY_FREDDIE_ENABLED,
        enabledByDefault = false,
    )
    val speakers = listOf(ELIZABETH, FREDDIE)

    fun preferences(context: Context): SharedPreferences =
        context.getSharedPreferences("battery_voice", Context.MODE_PRIVATE)

    fun speaker(id: String?): FamilySpeaker? = speakers.firstOrNull { it.id == id }

    fun speakerByAddress(address: String?): FamilySpeaker? =
        speakers.firstOrNull { it.address.equals(address, ignoreCase = true) }

    fun enabledKey(id: String?): String? = speaker(id)?.enabledKey

    fun isEnabled(context: Context, speaker: FamilySpeaker): Boolean =
        preferences(context).getBoolean(speaker.enabledKey, speaker.enabledByDefault)

    fun speechTemplate(context: Context): String {
        val value = preferences(context).getString(KEY_SPEECH_TEMPLATE, DEFAULT_SPEECH_TEMPLATE)
            ?.trim()
            ?.takeIf { it.isNotEmpty() }
            ?: DEFAULT_SPEECH_TEMPLATE
        return if (value == LEGACY_SPEECH_TEMPLATE) DEFAULT_SPEECH_TEMPLATE else value
    }

    fun deviceLabel(context: Context): String =
        preferences(context).getString(KEY_DEVICE_LABEL, null)
            ?.trim()
            ?.takeIf { it.isNotEmpty() }
            ?: if (context.resources.configuration.smallestScreenWidthDp >= 600) {
                "Android tablet"
            } else {
                "Android phone"
            }

    fun announcementVolumePercent(context: Context): Int =
        preferences(context).getInt(
            KEY_ANNOUNCEMENT_VOLUME_PERCENT,
            DEFAULT_ANNOUNCEMENT_VOLUME_PERCENT,
        ).coerceIn(20, 75)

    fun announcementVolumeIndex(
        currentVolume: Int,
        maximumVolume: Int,
        targetPercent: Int,
    ): Int = maxOf(
        currentVolume,
        maxOf(1, (maximumVolume * targetPercent.coerceIn(20, 75) + 99) / 100),
    )

    fun connectedDevicesPhrase(
        localDeviceLabel: String,
        sources: List<BoseConnectedSource>,
    ): String {
        val names = sources
            .map { if (it.isCurrentDevice) localDeviceLabel else it.name }
            .filter { it.isNotBlank() }
            .distinctBy { it.lowercase() }
        return when (names.size) {
            0 -> localDeviceLabel
            1 -> names[0]
            2 -> "${names[0]} and ${names[1]}"
            else -> names.dropLast(1).joinToString(", ") + ", and " + names.last()
        }
    }

    fun shouldAnnounceAutomatically(sources: List<BoseConnectedSource>): Boolean {
        if (sources.size < 2) return true
        val current = sources.firstOrNull { it.isCurrentDevice } ?: return true
        return current == automaticAnnouncer(sources)
    }

    fun automaticAnnouncer(sources: List<BoseConnectedSource>): BoseConnectedSource? =
        sources.maxByOrNull { it.name.lowercase(Locale.ROOT) }

    fun renderSpeech(
        context: Context,
        speaker: FamilySpeaker,
        level: Int,
        connectedSources: List<BoseConnectedSource> = emptyList(),
    ): String {
        val localDevice = deviceLabel(context)
        val connectedDevices = connectedDevicesPhrase(localDevice, connectedSources)
        return speechTemplate(context)
            .replace("{speaker}", speaker.name)
            .replace("{battery}", level.coerceIn(0, 100).toString())
            .replace("{devices}", connectedDevices)
            .replace("{device}", localDevice)
    }
}
