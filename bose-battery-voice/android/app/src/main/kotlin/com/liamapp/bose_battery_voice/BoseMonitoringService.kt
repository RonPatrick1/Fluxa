package com.liamapp.bose_battery_voice

import android.Manifest
import android.annotation.SuppressLint
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.bluetooth.BluetoothA2dp
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.media.AudioAttributes
import android.media.AudioDeviceInfo
import android.media.AudioFocusRequest
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioTrack
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import android.util.Log
import java.time.LocalTime
import java.time.format.DateTimeFormatter
import java.util.Locale
import java.util.concurrent.Executors

class BoseMonitoringService : Service(), TextToSpeech.OnInitListener {
    companion object {
        private const val TAG = "BoseBatteryVoice"
        const val ACTION_START = "com.liamapp.bose_battery_voice.START"
        const val ACTION_ANNOUNCE = "com.liamapp.bose_battery_voice.ANNOUNCE"
        const val ACTION_TEST_ACTIVE = "com.liamapp.bose_battery_voice.TEST_ACTIVE"
        const val EXTRA_SPEAKER_ID = "speaker_id"
        private const val CHANNEL_ID = "battery_voice_monitor"
        private const val NOTIFICATION_ID = 1042
        private const val ANNOUNCEMENT_COOLDOWN_MS = 60_000L
    }

    private data class PendingSpeech(
        val speaker: FamilySpeaker,
        val level: Int,
        val connectedDevices: String,
        val previousVolume: Int,
        val focusRequest: Any?,
    )

    private data class SpeechFocus(
        val granted: Boolean,
        val request: Any?,
    )

    private val handler = Handler(Looper.getMainLooper())
    private val executor = Executors.newSingleThreadExecutor()
    private var receiverRegistered = false
    private var textToSpeech: TextToSpeech? = null
    private var ttsReady = false
    private val pendingSpeech = mutableMapOf<String, PendingSpeech>()
    private val focusListener = AudioManager.OnAudioFocusChangeListener { }

    private val receiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            val device = deviceFrom(intent) ?: return
            val speaker = BatteryVoiceSettings.speakerByAddress(device.address) ?: return
            val connected = when (intent.action) {
                BluetoothA2dp.ACTION_CONNECTION_STATE_CHANGED ->
                    intent.getIntExtra(BluetoothProfile.EXTRA_STATE, -1) == BluetoothProfile.STATE_CONNECTED
                BluetoothDevice.ACTION_ACL_CONNECTED -> true
                else -> false
            }
            if (connected && BatteryVoiceSettings.isEnabled(this@BoseMonitoringService, speaker)) {
                record("${speaker.name} connected; waiting for its audio route")
                scheduleAnnouncement(speaker, force = false, attempt = 0)
            }
        }
    }

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
        startForeground(NOTIFICATION_ID, notification("Waiting for a selected Bose speaker"))
        textToSpeech = TextToSpeech(this, this)
        registerBluetoothReceiver()
        handler.postDelayed({ announceAlreadyConnectedSpeaker() }, 2_500)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_ANNOUNCE) {
            val speaker = BatteryVoiceSettings.speaker(intent.getStringExtra(EXTRA_SPEAKER_ID))
            if (speaker != null) scheduleAnnouncement(speaker, force = true, attempt = 0)
        } else if (intent?.action == ACTION_TEST_ACTIVE) {
            val speaker = BatteryVoiceSettings.speakers.firstOrNull { isActiveAudioRoute(it) }
            if (speaker == null) {
                record("Choose a family Bose as the active audio output before testing")
                stopIfEphemeral()
            } else {
                record("Testing the custom announcement on ${speaker.name}")
                scheduleAnnouncement(speaker, force = true, attempt = 0)
            }
        }
        return START_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onInit(status: Int) {
        ttsReady = status == TextToSpeech.SUCCESS
        if (ttsReady) {
            textToSpeech?.language = Locale.US
            textToSpeech?.setAudioAttributes(speechAudioAttributes())
            textToSpeech?.setOnUtteranceProgressListener(
                object : UtteranceProgressListener() {
                    override fun onStart(utteranceId: String) = Unit

                    override fun onDone(utteranceId: String) {
                        // TTS completion means Android has submitted the audio, but a
                        // Bluetooth speaker may still have about a second buffered.
                        // Keep focus and volume in place so the final word is not clipped.
                        handler.postDelayed(
                            { finishSpeech(utteranceId, success = true) },
                            1_200L,
                        )
                    }

                    @Deprecated("Required by Android's TTS callback")
                    override fun onError(utteranceId: String) {
                        handler.post { finishSpeech(utteranceId, success = false) }
                    }

                    override fun onError(utteranceId: String, errorCode: Int) {
                        handler.post {
                            finishSpeech(
                                utteranceId,
                                success = false,
                                detail = "error $errorCode",
                            )
                        }
                    }

                    override fun onStop(utteranceId: String, interrupted: Boolean) {
                        handler.post {
                            finishSpeech(utteranceId, success = false, detail = "interrupted")
                        }
                    }
                },
            )
        } else {
            record("Android text-to-speech could not initialize")
        }
    }

    override fun onDestroy() {
        if (receiverRegistered) unregisterReceiver(receiver)
        handler.removeCallbacksAndMessages(null)
        executor.shutdownNow()
        textToSpeech?.stop()
        pendingSpeech.keys.toList().forEach {
            finishSpeech(it, success = false, detail = "service stopped")
        }
        textToSpeech?.shutdown()
        super.onDestroy()
    }

    @SuppressLint("MissingPermission")
    private fun announceAlreadyConnectedSpeaker() {
        if (!hasBluetoothPermission()) return
        val adapter = (getSystemService(BLUETOOTH_SERVICE) as BluetoothManager).adapter ?: return
        BatteryVoiceSettings.speakers
            .filter { BatteryVoiceSettings.isEnabled(this, it) }
            .filter { isActiveAudioRoute(it) }
            .forEach { scheduleAnnouncement(it, force = false, attempt = 0) }
    }

    private fun scheduleAnnouncement(speaker: FamilySpeaker, force: Boolean, attempt: Int) {
        handler.postDelayed(
            { announceIfRouted(speaker, force, attempt) },
            if (attempt == 0) 2_500L else 1_000L,
        )
    }

    @SuppressLint("MissingPermission")
    private fun announceIfRouted(speaker: FamilySpeaker, force: Boolean, attempt: Int) {
        if (!hasBluetoothPermission()) {
            record("Bluetooth permission is missing")
            stopIfEphemeral()
            return
        }
        if (!isActiveAudioRoute(speaker)) {
            if (attempt < 8) {
                scheduleAnnouncement(speaker, force, attempt + 1)
            } else {
                record("${speaker.name} connected, but it was not the active audio output")
                stopIfEphemeral()
            }
            return
        }

        val preferences = BatteryVoiceSettings.preferences(this)
        val last = preferences.getLong(
            BatteryVoiceSettings.KEY_LAST_ANNOUNCEMENT_PREFIX + speaker.id,
            0L,
        )
        if (!force && System.currentTimeMillis() - last < ANNOUNCEMENT_COOLDOWN_MS) return

        val adapter: BluetoothAdapter =
            (getSystemService(BLUETOOTH_SERVICE) as BluetoothManager).adapter ?: return
        val device = adapter.getRemoteDevice(speaker.address)
        updateNotification("Reading ${speaker.name} battery")
        executor.execute {
            try {
                val snapshot = BoseBatteryReader.read(device)
                handler.post {
                    speak(
                        speaker,
                        snapshot.level.coerceIn(0, 100),
                        snapshot.connectedSources,
                    )
                }
            } catch (error: Exception) {
                record("Could not read ${speaker.name}: ${error.message ?: "Bluetooth error"}")
                stopIfEphemeral()
            }
        }
    }

    private fun speak(
        speaker: FamilySpeaker,
        level: Int,
        connectedSources: List<BoseConnectedSource>,
    ) {
        if (!ttsReady) {
            record("Battery $level%, but Android text-to-speech is not ready")
            stopIfEphemeral()
            return
        }
        if (!isActiveAudioRoute(speaker)) {
            record("Battery $level%, but ${speaker.name} stopped being the audio output")
            stopIfEphemeral()
            return
        }
        val audioManager = getSystemService(AUDIO_SERVICE) as AudioManager
        val focus = requestSpeechFocus(audioManager)
        if (!focus.granted) {
            record("Battery $level%, but Android denied temporary audio focus")
            return
        }

        val previousVolume = audioManager.getStreamVolume(AudioManager.STREAM_MUSIC)
        val maximumVolume = audioManager.getStreamMaxVolume(AudioManager.STREAM_MUSIC)
        val announcementVolume = maxOf(previousVolume, maxOf(1, maximumVolume / 3))
        if (announcementVolume != previousVolume) {
            audioManager.setStreamVolume(AudioManager.STREAM_MUSIC, announcementVolume, 0)
        }

        val utteranceId = "bose-${speaker.id}-${System.currentTimeMillis()}"
        val connectedDevices = BatteryVoiceSettings.connectedDevicesPhrase(
            BatteryVoiceSettings.deviceLabel(this),
            connectedSources,
        )
        pendingSpeech[utteranceId] = PendingSpeech(
            speaker = speaker,
            level = level,
            connectedDevices = connectedDevices,
            previousVolume = previousVolume,
            focusRequest = focus.request,
        )
        record("${speaker.name}: found $connectedDevices; preparing to speak $level%")
        handler.postDelayed(
            {
                if (!isActiveAudioRoute(speaker)) {
                    finishSpeech(
                        utteranceId,
                        success = false,
                        detail = "speaker stopped being the audio output",
                    )
                    return@postDelayed
                }
                val result = textToSpeech?.speak(
                    BatteryVoiceSettings.renderSpeech(
                        this,
                        speaker,
                        level,
                        connectedSources,
                    ),
                    TextToSpeech.QUEUE_FLUSH,
                    null,
                    utteranceId,
                ) ?: TextToSpeech.ERROR
                if (result != TextToSpeech.SUCCESS) {
                    finishSpeech(utteranceId, success = false, detail = "speech was rejected")
                }
            },
            300L,
        )
    }

    private fun finishSpeech(utteranceId: String, success: Boolean, detail: String? = null) {
        val pending = pendingSpeech.remove(utteranceId) ?: return
        val audioManager = getSystemService(AUDIO_SERVICE) as AudioManager
        audioManager.setStreamVolume(AudioManager.STREAM_MUSIC, pending.previousVolume, 0)
        abandonSpeechFocus(audioManager, pending.focusRequest)
        if (success) {
            BatteryVoiceSettings.preferences(this).edit()
                .putLong(
                    BatteryVoiceSettings.KEY_LAST_ANNOUNCEMENT_PREFIX + pending.speaker.id,
                    System.currentTimeMillis(),
                )
                .apply()
            record(
                "${pending.speaker.name}: announced ${pending.level}% for " +
                    "${pending.connectedDevices} at ${timestamp()}",
            )
        } else {
            record("Could not speak ${pending.level}%${detail?.let { ": $it" }.orEmpty()}")
        }
        stopIfEphemeral()
    }

    private fun requestSpeechFocus(audioManager: AudioManager): SpeechFocus {
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val request = AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT)
                .setAudioAttributes(speechAudioAttributes())
                .setOnAudioFocusChangeListener(focusListener, handler)
                .build()
            if (audioManager.requestAudioFocus(request) == AudioManager.AUDIOFOCUS_REQUEST_GRANTED) {
                SpeechFocus(granted = true, request = request)
            } else {
                SpeechFocus(granted = false, request = null)
            }
        } else {
            @Suppress("DEPRECATION")
            val result = audioManager.requestAudioFocus(
                focusListener,
                AudioManager.STREAM_MUSIC,
                AudioManager.AUDIOFOCUS_GAIN_TRANSIENT,
            )
            SpeechFocus(
                granted = result == AudioManager.AUDIOFOCUS_REQUEST_GRANTED,
                request = null,
            )
        }
    }

    private fun abandonSpeechFocus(audioManager: AudioManager, request: Any?) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O && request is AudioFocusRequest) {
            audioManager.abandonAudioFocusRequest(request)
        } else {
            @Suppress("DEPRECATION")
            audioManager.abandonAudioFocus(focusListener)
        }
    }

    private fun isActiveAudioRoute(speaker: FamilySpeaker): Boolean {
        // AudioManager's device list contains every connected output, not the
        // one media will actually use. A short silent AudioTrack gives us the
        // real routed device without changing volume or making a sound.
        val sampleRate = 16_000
        val minimum = AudioTrack.getMinBufferSize(
            sampleRate,
            AudioFormat.CHANNEL_OUT_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
        )
        if (minimum <= 0) return false
        val bufferSize = maxOf(minimum, sampleRate / 10 * 2)
        val track = try {
            AudioTrack.Builder()
                .setAudioAttributes(speechAudioAttributes())
                .setAudioFormat(
                    AudioFormat.Builder()
                        .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                        .setSampleRate(sampleRate)
                        .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                        .build(),
                )
                .setTransferMode(AudioTrack.MODE_STATIC)
                .setBufferSizeInBytes(bufferSize)
                .build()
        } catch (_: Exception) {
            return false
        }
        return try {
            track.write(ByteArray(bufferSize), 0, bufferSize)
            track.play()
            Thread.sleep(80)
            val device = track.routedDevice ?: return false
            val bluetoothOutput = device.type == AudioDeviceInfo.TYPE_BLUETOOTH_A2DP ||
                (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S &&
                    (device.type == AudioDeviceInfo.TYPE_BLE_SPEAKER ||
                        device.type == AudioDeviceInfo.TYPE_BLE_HEADSET))
            val addressMatches = device.address.equals(speaker.address, ignoreCase = true)
            bluetoothOutput &&
                (addressMatches || namesMatch(device.productName?.toString().orEmpty(), speaker.name))
        } catch (_: Exception) {
            false
        } finally {
            try {
                track.stop()
            } catch (_: Exception) {
            }
            track.release()
        }
    }

    private fun speechAudioAttributes(): AudioAttributes =
        AudioAttributes.Builder()
            .setUsage(AudioAttributes.USAGE_MEDIA)
            .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
            .build()

    private fun namesMatch(routeName: String, speakerName: String): Boolean {
        val route = routeName.lowercase().replace("’", "'")
        val expected = speakerName.lowercase().replace("’", "'")
        return route == expected || route.contains(expected) || expected.contains(route)
    }

    @Suppress("DEPRECATION")
    private fun deviceFrom(intent: Intent): BluetoothDevice? =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            intent.getParcelableExtra(BluetoothDevice.EXTRA_DEVICE, BluetoothDevice::class.java)
        } else {
            intent.getParcelableExtra(BluetoothDevice.EXTRA_DEVICE)
        }

    private fun registerBluetoothReceiver() {
        val filter = IntentFilter().apply {
            addAction(BluetoothDevice.ACTION_ACL_CONNECTED)
            addAction(BluetoothA2dp.ACTION_CONNECTION_STATE_CHANGED)
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            registerReceiver(receiver, filter, RECEIVER_EXPORTED)
        } else {
            @Suppress("DEPRECATION")
            registerReceiver(receiver, filter)
        }
        receiverRegistered = true
    }

    private fun record(message: String) {
        Log.i(TAG, message)
        BatteryVoiceSettings.preferences(this).edit()
            .putString(BatteryVoiceSettings.KEY_LAST_EVENT, message)
            .apply()
        updateNotification(message)
    }

    private fun timestamp(): String =
        LocalTime.now().format(DateTimeFormatter.ofPattern("h:mm a"))

    private fun stopIfEphemeral() {
        val monitoring = BatteryVoiceSettings.preferences(this)
            .getBoolean(BatteryVoiceSettings.KEY_MONITORING, false)
        if (!monitoring) stopSelf()
    }

    private fun hasBluetoothPermission(): Boolean =
        Build.VERSION.SDK_INT < Build.VERSION_CODES.S ||
            checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT) == PackageManager.PERMISSION_GRANTED

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val manager = getSystemService(NOTIFICATION_SERVICE) as NotificationManager
            manager.createNotificationChannel(
                NotificationChannel(
                    CHANNEL_ID,
                    "Bose battery monitoring",
                    NotificationManager.IMPORTANCE_LOW,
                ),
            )
        }
    }

    private fun notification(text: String): Notification {
        val openApp = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val builder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            Notification.Builder(this, CHANNEL_ID)
        } else {
            @Suppress("DEPRECATION")
            Notification.Builder(this)
        }
        return builder
            .setSmallIcon(android.R.drawable.stat_sys_data_bluetooth)
            .setContentTitle("Bose Battery Voice")
            .setContentText(text)
            .setContentIntent(openApp)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .build()
    }

    private fun updateNotification(text: String) {
        (getSystemService(NOTIFICATION_SERVICE) as NotificationManager)
            .notify(NOTIFICATION_ID, notification(text))
    }

}
