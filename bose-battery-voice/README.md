# Bose Battery Voice

A private, offline battery announcement helper for the family's two Bose
speakers:

- **Elizabeth's Bose** (SoundLink Max) is enabled by default because firmware
  8.2.16 no longer exposes the old onboard battery prompt.
- **Freddie's Bose** (SoundLink Flex Gen 2) is disabled by default because its
  onboard battery announcement still works.

The helper reads the battery percentage and the names/status of connected audio
sources. It does not use a Bose account, contact Bose, rename a speaker, change
speaker settings, or transfer firmware.

## Behavior

After a selected speaker connects, the helper verifies that it is the active
media output, reads the Bose battery value and its active multipoint sources,
and speaks the configured sentence. The default is
`{devices} connected to {speaker}. Battery {battery} percent.` The mobile app
accepts any text and expands `{speaker}`, `{battery}`, `{device}`, and
`{devices}` placeholders. `{device}` is the custom name of the phone or tablet
running the helper. `{devices}` contains both connected source names when Bose
reports a multipoint pair. For example, it can become
`Ron's phone and Ubuntu desktop connected to Elizabeth's Bose. Battery 80 percent.`
It will not
deliberately speak through the phone, tablet, computer, or the other Bose
speaker. A 60-second cooldown suppresses duplicate connection events.

When two devices running the helper receive the same new-connection event, they
elect one of the Bose-reported source names as the automatic announcer. That
helper names both sources in one sentence while the other stays quiet. Choosing
Ubuntu as the output after it is already connected still announces from Ubuntu.
Manual **Test** and Ubuntu `once` requests always run on the device where they
were requested.

On Android, if that speaker's remembered media volume is too low to hear, the
helper temporarily raises the media stream to a configurable target. The safer
default is 45% of Android's available volume steps (`7/15` on the tested Samsung
phone), because Samsung's Bluetooth loudness curve is nonlinear. The helper
verifies the applied index, then restores the prior volume if the user did not
change it during speech. Transient audio focus prevents
another app's media from playing at the temporary level. The app keeps the
audio route and volume in place for another 1.2 seconds after text-to-speech
finishes submitting audio, allowing the Bose Bluetooth buffer to play the last
word before the prior audio state is restored.

The Android and Ubuntu implementations use read-only BMAP over Bose's RFCOMM
control service. iOS cannot open arbitrary classic RFCOMM services, so it uses
the same read-only packets through Bose's BLE service (`FEBE`). If a speaker
does not support the optional source-name packets, the announcement falls back
to that helper's custom device name.

## Android phone and tablet

Build:

```sh
flutter build apk --debug
```

APK:

```text
build/app/outputs/flutter-apk/app-debug.apk
```

Install with ADB:

```sh
adb install -r build/app/outputs/flutter-apk/app-debug.apk
```

Open **Bose Battery Voice**, allow Nearby Devices, and turn on Monitoring.
The app then requests Android's **Unrestricted** battery mode when it is not
already granted and shows the current result in its Background reliability
card. Android requires the user to approve that protected system prompt.
On Samsung devices, use the card's shortcut to open Device Care's battery page,
then open **Background usage limits** → **Never sleeping apps**, tap **+**, and
add Bose Battery Voice. Samsung does not provide third-party apps an API for
silently editing that list.

Status notifications are off by default. On Android 13 and newer, denying the
optional notification permission keeps the required foreground-service entry
out of the notification drawer; Android may still show the running helper in
its system **Active apps** list. The app provides a shortcut to Android's
notification settings if an older installation had already granted permission.
The monitor restarts after a reboot if it was enabled. App updates restart the
monitor silently and suppress automatic speech until the currently connected
Bose disconnects; this prevents installing a build from becoming an audible
test.

## iPhone and iPad

Apple apps must be signed on macOS. On a Mac with Xcode and Flutter:

```sh
flutter pub get
open ios/Runner.xcworkspace
```

In Xcode, select the Runner target, choose the family's Apple development team
under **Signing & Capabilities**, select the attached iPhone or iPad, and Run.
Open the app once, allow Bluetooth, and turn on Monitoring.

The iPhone/iPad implementation does not create local or push notifications, so
there is no status item in Notification Center.

iOS speech uses the maximum `AVSpeechUtterance` gain. Apple exposes that as a
relative 0–1 speech level; the app does not override the user's system media
volume, so set an audible media volume before using **Test custom announcement**.

The iOS target declares Bluetooth-central restoration and background audio.
iOS can restore its pending BLE connection after normal system termination,
but swiping the app away disables that restoration until the app is opened
again.

## Ubuntu desktop

The user service is installed with:

```sh
./ubuntu/install.sh
```

It monitors only Elizabeth's Bose. It starts silently: installing or restarting
the service never announces a speaker that was already active. It announces
when Elizabeth's Bose subsequently becomes the default PipeWire/PulseAudio
output, including when the output is selected after Bluetooth connects.

Useful commands:

```sh
bose-battery-voice status
bose-battery-voice settings
systemctl --user status bose-battery-voice.service
journalctl --user -u bose-battery-voice.service
```

Ubuntu settings are stored in
`~/.config/bose-battery-voice/settings.ini`. Show the effective values or change
them without reinstalling the service:

```sh
bose-battery-voice settings \
  --device-label "Alien3 Ubuntu" \
  --volume 45 \
  --template "{devices} connected to {speaker}. Battery {battery} percent."
```

The service reloads these values for each announcement. It temporarily raises a
quiet default-sink volume to the configured percentage and restores it after
speech.

The following command is intentionally audible and only succeeds while
Elizabeth's Bose is connected and active:

```sh
bose-battery-voice once elizabeth
```

Remove the service with `./ubuntu/uninstall.sh`.

## Verification

```sh
flutter analyze
flutter test
(cd android && ./gradlew testDebugUnitTest)
python3 -m unittest -v ubuntu/test_bose_battery_voice.py
```

The iOS source cannot be compiled or signed on Ubuntu; its final device build
must be validated in Xcode.
