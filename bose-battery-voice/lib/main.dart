import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(const BatteryVoiceApp());
}

class BatteryVoiceApp extends StatelessWidget {
  const BatteryVoiceApp({super.key});

  @override
  Widget build(BuildContext context) {
    const navy = Color(0xFF102A43);
    return MaterialApp(
      debugShowCheckedModeBanner: false,
      title: 'Bose Battery Voice',
      theme: ThemeData(
        colorScheme: ColorScheme.fromSeed(
          seedColor: navy,
          brightness: Brightness.light,
        ),
        scaffoldBackgroundColor: const Color(0xFFF4F7FA),
        cardTheme: const CardThemeData(
          elevation: 0,
          margin: EdgeInsets.zero,
          color: Colors.white,
        ),
        useMaterial3: true,
      ),
      home: const BatteryVoiceHome(),
    );
  }
}

class BatteryVoiceHome extends StatefulWidget {
  const BatteryVoiceHome({super.key});

  @override
  State<BatteryVoiceHome> createState() => _BatteryVoiceHomeState();
}

class _BatteryVoiceHomeState extends State<BatteryVoiceHome>
    with WidgetsBindingObserver {
  static const _channel = MethodChannel(
    'com.liamapp.bose_battery_voice/control',
  );

  Map<String, dynamic> _status = const {};
  bool _loading = true;
  bool _backgroundSetupRequested = false;
  String? _error;
  Timer? _refreshTimer;

  bool get _monitoring => _status['monitoring'] == true;
  bool get _elizabethEnabled => _status['elizabethEnabled'] != false;
  bool get _freddieEnabled => _status['freddieEnabled'] == true;
  bool get _showStatusNotifications =>
      _status['showStatusNotifications'] == true;
  bool get _notificationPermissionGranted =>
      _status['notificationPermissionGranted'] == true;
  bool get _batteryOptimizationIgnored =>
      _status['batteryOptimizationIgnored'] == true;
  bool get _isSamsung => _status['isSamsung'] == true;
  String get _speechTemplate =>
      _status['speechTemplate']?.toString() ??
      '{devices} connected to {speaker}. Battery {battery} percent.';
  String get _deviceLabel =>
      _status['deviceLabel']?.toString() ??
      (Platform.isIOS ? 'iPhone' : 'Android phone');
  int get _announcementVolumePercent =>
      (_status['announcementVolumePercent'] as num?)?.round() ?? 45;

  String get _announcementPreview => _speechTemplate
      .replaceAll('{speaker}', "Elizabeth's Bose")
      .replaceAll('{battery}', '100')
      .replaceAll('{devices}', _deviceLabel)
      .replaceAll('{device}', _deviceLabel);

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _refresh();
    _refreshTimer = Timer.periodic(
      const Duration(seconds: 4),
      (_) => _refresh(quiet: true),
    );
  }

  @override
  void dispose() {
    _refreshTimer?.cancel();
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) {
      _refresh(quiet: true);
    }
  }

  Future<void> _refresh({bool quiet = false}) async {
    if (!quiet && mounted) {
      setState(() => _loading = true);
    }
    try {
      final value = await _channel.invokeMapMethod<String, dynamic>(
        'getStatus',
      );
      if (!mounted) return;
      final nextStatus = value ?? const <String, dynamic>{};
      final shouldRequestBackgroundSetup =
          !_backgroundSetupRequested &&
          nextStatus['platform'] == 'Android' &&
          nextStatus['monitoring'] == true &&
          nextStatus['batteryOptimizationIgnored'] != true;
      if (shouldRequestBackgroundSetup) {
        _backgroundSetupRequested = true;
      }
      setState(() {
        _status = nextStatus;
        _error = null;
        _loading = false;
      });
      if (shouldRequestBackgroundSetup) {
        WidgetsBinding.instance.addPostFrameCallback((_) {
          if (mounted) _requestUnrestrictedBattery();
        });
      }
    } on PlatformException catch (error) {
      if (!mounted) return;
      setState(() {
        _error = error.message ?? error.code;
        _loading = false;
      });
    }
  }

  Future<void> _requestPermissions() async {
    try {
      await _channel.invokeMethod<bool>('requestPermissions');
      await _refresh();
    } on PlatformException catch (error) {
      _show(error.message ?? error.code);
    }
  }

  Future<void> _setMonitoring(bool value) async {
    try {
      final allowed = await _channel.invokeMethod<bool>('requestPermissions');
      if (allowed != true) {
        _show('Bluetooth permission is required.');
        return;
      }
      await _channel.invokeMethod<void>('setMonitoring', {'enabled': value});
      if (value && _status['platform'] == 'Android') {
        _backgroundSetupRequested = true;
        await _requestUnrestrictedBattery();
      }
      await _refresh();
    } on PlatformException catch (error) {
      _show(error.message ?? error.code);
      await _refresh(quiet: true);
    }
  }

  Future<void> _setSpeaker(String id, bool value) async {
    await _channel.invokeMethod<void>('setSpeakerEnabled', {
      'id': id,
      'enabled': value,
    });
    await _refresh(quiet: true);
  }

  Future<void> _announceNow(String id) async {
    try {
      await _channel.invokeMethod<void>('announceNow', {'id': id});
      _show('Battery check started. Keep the speaker connected for a moment.');
      await Future<void>.delayed(const Duration(seconds: 2));
      await _refresh(quiet: true);
    } on PlatformException catch (error) {
      _show(error.message ?? error.code);
    }
  }

  Future<void> _testAnnouncement() async {
    try {
      await _channel.invokeMethod<void>('testAnnouncement');
      _show(
        'Custom announcement test started on the active family Bose speaker.',
      );
      await Future<void>.delayed(const Duration(seconds: 2));
      await _refresh(quiet: true);
    } on PlatformException catch (error) {
      _show(error.message ?? error.code);
    }
  }

  Future<void> _setStatusNotifications(bool value) async {
    try {
      final effective = await _channel.invokeMethod<bool>(
        'setStatusNotifications',
        {'enabled': value},
      );
      await _refresh(quiet: true);
      if (!value && effective != true && Platform.isAndroid) {
        _show('Switch off notifications on the Android app settings screen.');
        await _openNotificationSettings();
      }
    } on PlatformException catch (error) {
      _show(error.message ?? error.code);
      await _refresh(quiet: true);
    }
  }

  Future<void> _openNotificationSettings() async {
    try {
      await _channel.invokeMethod<void>('openNotificationSettings');
    } on PlatformException catch (error) {
      _show(error.message ?? error.code);
    }
  }

  Future<void> _requestUnrestrictedBattery() async {
    try {
      final opened = await _channel.invokeMethod<bool>(
        'requestUnrestrictedBattery',
      );
      if (opened == false) {
        _show('Android could not open battery settings on this device.');
      }
    } on PlatformException catch (error) {
      _show(error.message ?? error.code);
    }
  }

  Future<void> _openSamsungBackgroundSettings() async {
    try {
      final opened = await _channel.invokeMethod<bool>(
        'openSamsungBackgroundSettings',
      );
      if (opened == false) {
        _show('Android could not open background battery settings.');
      }
    } on PlatformException catch (error) {
      _show(error.message ?? error.code);
    }
  }

  Future<void> _editAnnouncement() async {
    final settings = await showDialog<_AnnouncementSettings>(
      context: context,
      builder: (context) => _AnnouncementEditorDialog(
        initialTemplate: _speechTemplate,
        initialDeviceLabel: _deviceLabel,
        initialVolumePercent: _announcementVolumePercent,
        showVolume: _status['platform'] == 'Android',
      ),
    );
    if (settings == null || !mounted) return;
    try {
      await _channel.invokeMethod<void>('setSpeechSettings', {
        'template': settings.template,
        'deviceLabel': settings.deviceLabel,
        'announcementVolumePercent': settings.volumePercent,
      });
      await _refresh(quiet: true);
      _show('Announcement updated.');
    } on PlatformException catch (error) {
      _show(error.message ?? error.code);
    }
  }

  void _show(String message) {
    if (!mounted) return;
    ScaffoldMessenger.of(context)
      ..hideCurrentSnackBar()
      ..showSnackBar(SnackBar(content: Text(message)));
  }

  @override
  Widget build(BuildContext context) {
    final platform =
        _status['platform']?.toString() ?? (Platform.isIOS ? 'iOS' : 'Android');
    final lastEvent = _status['lastEvent']?.toString();

    return Scaffold(
      appBar: AppBar(
        title: const Text('Bose Battery Voice'),
        actions: [
          IconButton(
            tooltip: 'Refresh',
            onPressed: _refresh,
            icon: const Icon(Icons.refresh),
          ),
        ],
      ),
      body: _loading && _status.isEmpty
          ? const Center(child: CircularProgressIndicator())
          : ListView(
              padding: const EdgeInsets.fromLTRB(18, 8, 18, 32),
              children: [
                _HeroCard(
                  monitoring: _monitoring,
                  platform: platform,
                  onChanged: _setMonitoring,
                ),
                const SizedBox(height: 18),
                _AnnouncementCard(
                  preview: _announcementPreview,
                  deviceLabel: _deviceLabel,
                  showStatusNotifications: _showStatusNotifications,
                  notificationPermissionGranted: _notificationPermissionGranted,
                  platform: platform,
                  onEdit: _editAnnouncement,
                  onTest: _testAnnouncement,
                  onNotificationsChanged: _setStatusNotifications,
                  onOpenNotificationSettings: _openNotificationSettings,
                ),
                if (platform == 'Android') ...[
                  const SizedBox(height: 18),
                  _BackgroundReliabilityCard(
                    unrestricted: _batteryOptimizationIgnored,
                    isSamsung: _isSamsung,
                    onRequestUnrestricted: _requestUnrestrictedBattery,
                    onOpenSamsungSettings: _openSamsungBackgroundSettings,
                  ),
                ],
                const SizedBox(height: 18),
                Text('Speakers', style: Theme.of(context).textTheme.titleLarge),
                const SizedBox(height: 10),
                _SpeakerCard(
                  name: "Elizabeth's Bose",
                  subtitle: 'SoundLink Max · restored phone announcement',
                  enabled: _elizabethEnabled,
                  onChanged: (value) => _setSpeaker('elizabeth', value),
                  onTest: () => _announceNow('elizabeth'),
                ),
                const SizedBox(height: 10),
                _SpeakerCard(
                  name: "Freddie's Bose",
                  subtitle: 'Flex Gen 2 · onboard announcement already works',
                  enabled: _freddieEnabled,
                  onChanged: (value) => _setSpeaker('freddie', value),
                  onTest: () => _announceNow('freddie'),
                ),
                const SizedBox(height: 18),
                Card(
                  child: Padding(
                    padding: const EdgeInsets.all(16),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          'Status',
                          style: Theme.of(context).textTheme.titleMedium,
                        ),
                        const SizedBox(height: 8),
                        Text(
                          lastEvent?.isNotEmpty == true
                              ? lastEvent!
                              : 'No announcement recorded yet.',
                        ),
                        if (_error != null) ...[
                          const SizedBox(height: 8),
                          Text(
                            _error!,
                            style: const TextStyle(color: Colors.red),
                          ),
                        ],
                        const SizedBox(height: 12),
                        OutlinedButton.icon(
                          onPressed: _requestPermissions,
                          icon: const Icon(Icons.bluetooth),
                          label: const Text('Check Bluetooth permission'),
                        ),
                      ],
                    ),
                  ),
                ),
                const SizedBox(height: 14),
                const Text(
                  'The app reads the battery and connected device names. It never '
                  'contacts Bose, changes settings, or transfers firmware.',
                  textAlign: TextAlign.center,
                  style: TextStyle(color: Color(0xFF627D98)),
                ),
              ],
            ),
    );
  }
}

class _AnnouncementSettings {
  const _AnnouncementSettings({
    required this.template,
    required this.deviceLabel,
    required this.volumePercent,
  });

  final String template;
  final String deviceLabel;
  final int volumePercent;
}

class _AnnouncementEditorDialog extends StatefulWidget {
  const _AnnouncementEditorDialog({
    required this.initialTemplate,
    required this.initialDeviceLabel,
    required this.initialVolumePercent,
    required this.showVolume,
  });

  final String initialTemplate;
  final String initialDeviceLabel;
  final int initialVolumePercent;
  final bool showVolume;

  @override
  State<_AnnouncementEditorDialog> createState() =>
      _AnnouncementEditorDialogState();
}

class _AnnouncementEditorDialogState extends State<_AnnouncementEditorDialog> {
  late final TextEditingController _templateController;
  late final TextEditingController _deviceController;
  late double _volumePercent;
  String? _validationError;

  @override
  void initState() {
    super.initState();
    _templateController = TextEditingController(text: widget.initialTemplate);
    _deviceController = TextEditingController(text: widget.initialDeviceLabel);
    _volumePercent = widget.initialVolumePercent.toDouble();
  }

  @override
  void dispose() {
    _templateController.dispose();
    _deviceController.dispose();
    super.dispose();
  }

  void _save() {
    final template = _templateController.text.trim();
    final deviceLabel = _deviceController.text.trim();
    if (template.isEmpty || deviceLabel.isEmpty) {
      setState(() => _validationError = 'Both fields need some text.');
      return;
    }
    Navigator.of(
      context,
    ).pop(
      _AnnouncementSettings(
        template: template,
        deviceLabel: deviceLabel,
        volumePercent: _volumePercent.round(),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: const Text('Customize announcement'),
      content: SingleChildScrollView(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            TextField(
              controller: _deviceController,
              maxLength: 80,
              decoration: const InputDecoration(
                labelText: 'This device name',
                hintText: "Ron's phone",
              ),
            ),
            if (widget.showVolume) ...[
              const SizedBox(height: 12),
              Text(
                'Minimum Android volume step: ${_volumePercent.round()}%',
              ),
              Slider(
                min: 20,
                max: 75,
                divisions: 11,
                value: _volumePercent,
                label: '${_volumePercent.round()}%',
                onChanged: (value) => setState(() => _volumePercent = value),
              ),
              const Text(
                'Samsung Bluetooth volume is nonlinear. Start low and use Test to adjust it.',
                style: TextStyle(color: Color(0xFF627D98)),
              ),
            ],
            const SizedBox(height: 8),
            TextField(
              controller: _templateController,
              maxLength: 240,
              minLines: 2,
              maxLines: 5,
              decoration: const InputDecoration(
                labelText: 'What it should say',
                helperText:
                    'Use {devices} for every connected source; {device} means this device. '
                    'Also supports {speaker} and {battery}.',
                helperMaxLines: 2,
                alignLabelWithHint: true,
              ),
            ),
            if (_validationError != null) ...[
              const SizedBox(height: 8),
              Text(
                _validationError!,
                style: TextStyle(color: Theme.of(context).colorScheme.error),
              ),
            ],
          ],
        ),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.of(context).pop(),
          child: const Text('Cancel'),
        ),
        FilledButton(onPressed: _save, child: const Text('Save')),
      ],
    );
  }
}

class _HeroCard extends StatelessWidget {
  const _HeroCard({
    required this.monitoring,
    required this.platform,
    required this.onChanged,
  });

  final bool monitoring;
  final String platform;
  final ValueChanged<bool> onChanged;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(20),
      decoration: BoxDecoration(
        color: const Color(0xFF102A43),
        borderRadius: BorderRadius.circular(24),
      ),
      child: Row(
        children: [
          CircleAvatar(
            radius: 28,
            backgroundColor: Colors.white.withValues(alpha: 0.12),
            child: const Icon(Icons.record_voice_over, color: Colors.white),
          ),
          const SizedBox(width: 16),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  monitoring ? 'Listening for your Bose' : 'Monitoring is off',
                  style: const TextStyle(
                    color: Colors.white,
                    fontWeight: FontWeight.w700,
                    fontSize: 18,
                  ),
                ),
                const SizedBox(height: 4),
                Text(
                  '$platform · speaks after the audio connection is ready',
                  style: const TextStyle(color: Color(0xFFBCCCDC)),
                ),
              ],
            ),
          ),
          Switch(value: monitoring, onChanged: onChanged),
        ],
      ),
    );
  }
}

class _AnnouncementCard extends StatelessWidget {
  const _AnnouncementCard({
    required this.preview,
    required this.deviceLabel,
    required this.showStatusNotifications,
    required this.notificationPermissionGranted,
    required this.platform,
    required this.onEdit,
    required this.onTest,
    required this.onNotificationsChanged,
    required this.onOpenNotificationSettings,
  });

  final String preview;
  final String deviceLabel;
  final bool showStatusNotifications;
  final bool notificationPermissionGranted;
  final String platform;
  final VoidCallback onEdit;
  final VoidCallback onTest;
  final ValueChanged<bool> onNotificationsChanged;
  final VoidCallback onOpenNotificationSettings;

  @override
  Widget build(BuildContext context) {
    final isIOS = platform == 'iOS';
    final needsAndroidSettings =
        !isIOS && !showStatusNotifications && notificationPermissionGranted;
    return Card(
      child: Padding(
        padding: const EdgeInsets.fromLTRB(16, 16, 16, 10),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Expanded(
                  child: Text(
                    'Announcement',
                    style: Theme.of(context).textTheme.titleMedium,
                  ),
                ),
                TextButton.icon(
                  onPressed: onEdit,
                  icon: const Icon(Icons.edit, size: 18),
                  label: const Text('Customize'),
                ),
              ],
            ),
            Text(
              '“$preview”',
              style: const TextStyle(
                color: Color(0xFF334E68),
                fontStyle: FontStyle.italic,
              ),
            ),
            const SizedBox(height: 4),
            Text(
              'This device: $deviceLabel',
              style: const TextStyle(color: Color(0xFF627D98)),
            ),
            const SizedBox(height: 3),
            const Text(
              '{devices} automatically adds the second multipoint source when connected.',
              style: TextStyle(color: Color(0xFF627D98)),
            ),
            const SizedBox(height: 3),
            Text(
              isIOS
                  ? 'iPhone and iPad use the current system media volume.'
                  : 'Android temporarily raises quiet media volume to your saved target, then restores it.',
              style: const TextStyle(color: Color(0xFF627D98)),
            ),
            const SizedBox(height: 10),
            OutlinedButton.icon(
              onPressed: onTest,
              icon: const Icon(Icons.volume_up, size: 18),
              label: const Text('Test custom announcement'),
            ),
            const Divider(height: 24),
            SwitchListTile(
              contentPadding: EdgeInsets.zero,
              title: const Text('Status notifications'),
              subtitle: Text(
                isIOS
                    ? 'iPhone and iPad do not post status notifications.'
                    : needsAndroidSettings
                    ? 'Off in this app; Android notification permission is still on.'
                    : 'Off by default. Monitoring continues in the background.',
              ),
              value: isIOS ? false : showStatusNotifications,
              onChanged: isIOS ? null : onNotificationsChanged,
            ),
            if (needsAndroidSettings)
              TextButton(
                onPressed: onOpenNotificationSettings,
                child: const Text('Open Android notification settings'),
              ),
          ],
        ),
      ),
    );
  }
}

class _BackgroundReliabilityCard extends StatelessWidget {
  const _BackgroundReliabilityCard({
    required this.unrestricted,
    required this.isSamsung,
    required this.onRequestUnrestricted,
    required this.onOpenSamsungSettings,
  });

  final bool unrestricted;
  final bool isSamsung;
  final VoidCallback onRequestUnrestricted;
  final VoidCallback onOpenSamsungSettings;

  @override
  Widget build(BuildContext context) {
    final statusColor = unrestricted
        ? const Color(0xFF16865C)
        : Theme.of(context).colorScheme.error;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              'Background reliability',
              style: Theme.of(context).textTheme.titleMedium,
            ),
            const SizedBox(height: 10),
            Row(
              children: [
                Icon(
                  unrestricted ? Icons.check_circle : Icons.warning_amber,
                  color: statusColor,
                ),
                const SizedBox(width: 10),
                Expanded(
                  child: Text(
                    unrestricted
                        ? 'Android battery mode: Unrestricted'
                        : 'Android battery mode needs Unrestricted access',
                    style: TextStyle(
                      color: statusColor,
                      fontWeight: FontWeight.w700,
                    ),
                  ),
                ),
              ],
            ),
            if (!unrestricted) ...[
              const SizedBox(height: 10),
              FilledButton.icon(
                onPressed: onRequestUnrestricted,
                icon: const Icon(Icons.battery_saver),
                label: const Text('Allow unrestricted battery'),
              ),
            ],
            if (isSamsung) ...[
              const SizedBox(height: 14),
              const Text(
                'Samsung requires confirmation in Background usage limits. '
                'Open Never sleeping apps, tap +, and add Bose Battery Voice.',
                style: TextStyle(color: Color(0xFF627D98)),
              ),
              const SizedBox(height: 8),
              OutlinedButton.icon(
                onPressed: onOpenSamsungSettings,
                icon: const Icon(Icons.open_in_new),
                label: const Text('Open Samsung sleeping settings'),
              ),
            ],
          ],
        ),
      ),
    );
  }
}

class _SpeakerCard extends StatelessWidget {
  const _SpeakerCard({
    required this.name,
    required this.subtitle,
    required this.enabled,
    required this.onChanged,
    required this.onTest,
  });

  final String name;
  final String subtitle;
  final bool enabled;
  final ValueChanged<bool> onChanged;
  final VoidCallback onTest;

  @override
  Widget build(BuildContext context) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.fromLTRB(16, 12, 10, 12),
        child: Row(
          children: [
            const Icon(Icons.speaker, color: Color(0xFF486581)),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    name,
                    style: const TextStyle(fontWeight: FontWeight.w700),
                  ),
                  const SizedBox(height: 2),
                  Text(
                    subtitle,
                    style: const TextStyle(color: Color(0xFF627D98)),
                  ),
                  const SizedBox(height: 8),
                  TextButton.icon(
                    onPressed: onTest,
                    icon: const Icon(Icons.volume_up, size: 18),
                    label: const Text('Test now'),
                  ),
                ],
              ),
            ),
            Switch(value: enabled, onChanged: onChanged),
          ],
        ),
      ),
    );
  }
}
