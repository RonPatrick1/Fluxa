import AVFoundation
import CoreBluetooth
import Flutter
import UIKit

private struct FamilySpeaker {
  let id: String
  let name: String
  let enabledKey: String
  let enabledByDefault: Bool
}

private struct BoseConnectedSource {
  let name: String
  let isCurrentDevice: Bool
}

private final class BoseBatteryCoordinator: NSObject {
  private static let channelName = "com.liamapp.bose_battery_voice/control"
  private static let monitoringKey = "monitoring"
  private static let lastEventKey = "last_event"
  private static let speechTemplateKey = "speech_template"
  private static let deviceLabelKey = "device_label"
  private static let legacySpeechTemplate = "Battery {battery} percent."
  private static let defaultSpeechTemplate =
    "{devices} connected to {speaker}. Battery {battery} percent."
  private static let boseService = CBUUID(string: "FEBE")
  private static let secureCharacteristic = CBUUID(
    string: "C65B8F2F-AEE2-4C89-B758-BC4892D6F2D8"
  )
  private static let fallbackCharacteristic = CBUUID(
    string: "D417C028-9818-4354-99D1-2AC09D074591"
  )
  private static let restoreIdentifier = "com.liamapp.boseBatteryVoice.central"
  private static let cooldown: TimeInterval = 60

  private let speakers = [
    FamilySpeaker(
      id: "elizabeth",
      name: "Elizabeth's Bose",
      enabledKey: "elizabeth_enabled",
      enabledByDefault: true
    ),
    FamilySpeaker(
      id: "freddie",
      name: "Freddie's Bose",
      enabledKey: "freddie_enabled",
      enabledByDefault: false
    ),
  ]
  private let defaults = UserDefaults.standard
  private var central: CBCentralManager!
  private var channel: FlutterMethodChannel?
  private var peripherals: [UUID: CBPeripheral] = [:]
  private var speakerIDsByPeripheral: [UUID: String] = [:]
  private var controlCharacteristics: [UUID: CBCharacteristic] = [:]
  private var receiveBuffers: [UUID: Data] = [:]
  private var pendingBatteryLevels: [UUID: Int] = [:]
  private var pendingSourceAddresses: [UUID: [Data]] = [:]
  private var pendingConnectedSources: [UUID: [BoseConnectedSource]] = [:]
  private var forceRequests = Set<String>()
  private var announcementsInFlight = Set<String>()
  private var utteranceSpeakerIDs: [ObjectIdentifier: String] = [:]
  private let synthesizer = AVSpeechSynthesizer()

  override init() {
    super.init()
    synthesizer.delegate = self
    central = CBCentralManager(
      delegate: self,
      queue: nil,
      options: [CBCentralManagerOptionRestoreIdentifierKey: Self.restoreIdentifier]
    )
  }

  func register(messenger: FlutterBinaryMessenger) {
    let channel = FlutterMethodChannel(name: Self.channelName, binaryMessenger: messenger)
    channel.setMethodCallHandler { [weak self] call, result in
      guard let self else {
        result(FlutterError(code: "unavailable", message: "Battery monitor unavailable", details: nil))
        return
      }
      self.handle(call, result: result)
    }
    self.channel = channel
  }

  private func handle(_ call: FlutterMethodCall, result: @escaping FlutterResult) {
    switch call.method {
    case "getStatus":
      result(status())
    case "requestPermissions":
      result(CBManager.authorization != .denied && CBManager.authorization != .restricted)
    case "setStatusNotifications":
      // This app does not create iOS local or push notifications. Keep the
      // shared control callable while reporting that notifications remain off.
      result(true)
    case "setSpeechSettings":
      guard let arguments = call.arguments as? [String: Any],
            let template = (arguments["template"] as? String)?.trimmingCharacters(
              in: .whitespacesAndNewlines
            ),
            let deviceLabel = (arguments["deviceLabel"] as? String)?.trimmingCharacters(
              in: .whitespacesAndNewlines
            ),
            !template.isEmpty, template.count <= 240,
            !deviceLabel.isEmpty, deviceLabel.count <= 80 else {
        result(FlutterError(
          code: "speech",
          message: "Enter an announcement and a device name.",
          details: nil
        ))
        return
      }
      defaults.set(template, forKey: Self.speechTemplateKey)
      defaults.set(deviceLabel, forKey: Self.deviceLabelKey)
      result(nil)
    case "setMonitoring":
      guard let arguments = call.arguments as? [String: Any],
            let enabled = arguments["enabled"] as? Bool else {
        result(invalidArguments())
        return
      }
      defaults.set(enabled, forKey: Self.monitoringKey)
      if enabled {
        startScanning()
      } else {
        stopMonitoringConnections()
      }
      result(nil)
    case "setSpeakerEnabled":
      guard let arguments = call.arguments as? [String: Any],
            let id = arguments["id"] as? String,
            let enabled = arguments["enabled"] as? Bool,
            let speaker = speaker(id: id) else {
        result(invalidArguments())
        return
      }
      defaults.set(enabled, forKey: speaker.enabledKey)
      if enabled { startScanning() }
      result(nil)
    case "announceNow":
      guard let arguments = call.arguments as? [String: Any],
            let id = arguments["id"] as? String,
            speaker(id: id) != nil else {
        result(invalidArguments())
        return
      }
      beginAnnouncement(for: speaker(id: id)!, forced: true)
      result(nil)
    case "testAnnouncement":
      guard let activeSpeaker = speakers.first(where: { activeAudioRouteMatches($0) }) else {
        result(FlutterError(
          code: "audio_route",
          message: "Choose Elizabeth's Bose or Freddie's Bose as the audio output before testing.",
          details: nil
        ))
        return
      }
      beginAnnouncement(for: activeSpeaker, forced: true)
      result(nil)
    default:
      result(FlutterMethodNotImplemented)
    }
  }

  private func status() -> [String: Any] {
    [
      "platform": "iOS",
      "monitoring": defaults.bool(forKey: Self.monitoringKey),
      "showStatusNotifications": false,
      "notificationPermissionGranted": false,
      "speechTemplate": speechTemplate,
      "deviceLabel": deviceLabel,
      "elizabethEnabled": isEnabled(speaker(id: "elizabeth")!),
      "freddieEnabled": isEnabled(speaker(id: "freddie")!),
      "lastEvent": defaults.string(forKey: Self.lastEventKey) ?? "",
      "bluetoothAuthorization": authorizationDescription(),
    ]
  }

  private func invalidArguments() -> FlutterError {
    FlutterError(code: "invalid_arguments", message: "Invalid speaker setting", details: nil)
  }

  private var speechTemplate: String {
    let value = defaults.string(forKey: Self.speechTemplateKey)?
      .trimmingCharacters(in: .whitespacesAndNewlines)
    guard value?.isEmpty == false, value != Self.legacySpeechTemplate else {
      return Self.defaultSpeechTemplate
    }
    return value!
  }

  private var deviceLabel: String {
    let value = defaults.string(forKey: Self.deviceLabelKey)?
      .trimmingCharacters(in: .whitespacesAndNewlines)
    return value?.isEmpty == false ? value! : UIDevice.current.model
  }

  private func connectedDevicesPhrase(_ sources: [BoseConnectedSource]) -> String {
    var names: [String] = []
    for source in sources {
      let name = source.isCurrentDevice ? deviceLabel : source.name
      if !name.isEmpty && !names.contains(where: {
        $0.caseInsensitiveCompare(name) == .orderedSame
      }) {
        names.append(name)
      }
    }
    switch names.count {
    case 0: return deviceLabel
    case 1: return names[0]
    case 2: return "\(names[0]) and \(names[1])"
    default: return names.dropLast().joined(separator: ", ") + ", and " + names.last!
    }
  }

  private func automaticAnnouncer(_ sources: [BoseConnectedSource]) -> BoseConnectedSource? {
    sources.max { $0.name.lowercased() < $1.name.lowercased() }
  }

  private func shouldAnnounceAutomatically(_ sources: [BoseConnectedSource]) -> Bool {
    guard sources.count >= 2,
          let current = sources.first(where: { $0.isCurrentDevice }),
          let selected = automaticAnnouncer(sources) else { return true }
    return current.name.caseInsensitiveCompare(selected.name) == .orderedSame
  }

  private func renderedSpeech(
    for speaker: FamilySpeaker,
    level: Int,
    connectedSources: [BoseConnectedSource]
  ) -> String {
    speechTemplate
      .replacingOccurrences(of: "{speaker}", with: speaker.name)
      .replacingOccurrences(of: "{battery}", with: String(max(0, min(level, 100))))
      .replacingOccurrences(of: "{devices}", with: connectedDevicesPhrase(connectedSources))
      .replacingOccurrences(of: "{device}", with: deviceLabel)
  }

  private func speaker(id: String) -> FamilySpeaker? {
    speakers.first { $0.id == id }
  }

  private func isEnabled(_ speaker: FamilySpeaker) -> Bool {
    if defaults.object(forKey: speaker.enabledKey) == nil {
      return speaker.enabledByDefault
    }
    return defaults.bool(forKey: speaker.enabledKey)
  }

  private func shouldUse(_ speaker: FamilySpeaker) -> Bool {
    (defaults.bool(forKey: Self.monitoringKey) && isEnabled(speaker))
      || forceRequests.contains(speaker.id)
  }

  private func beginAnnouncement(for speaker: FamilySpeaker, forced: Bool) {
    guard !announcementsInFlight.contains(speaker.id) else {
      let alreadySpeaking = utteranceSpeakerIDs.values.contains(speaker.id)
      if forced && !alreadySpeaking {
        forceRequests.insert(speaker.id)
        record("The pending \(speaker.name) announcement is now a manual test")
      } else {
        record("\(speaker.name) already has an announcement in progress")
      }
      return
    }
    if forced { forceRequests.insert(speaker.id) }
    announcementsInFlight.insert(speaker.id)
    record(forced ? "Testing the custom announcement on \(speaker.name)" :
      "Looking for \(speaker.name)")
    if let existing = peripherals.first(where: {
      speakerIDsByPeripheral[$0.key] == speaker.id && $0.value.state == .connected
    })?.value {
      existing.delegate = self
      receiveBuffers[existing.identifier] = Data()
      existing.discoverServices([Self.boseService])
    } else {
      startScanning()
    }
  }

  private func completeAnnouncement(for speaker: FamilySpeaker) {
    announcementsInFlight.remove(speaker.id)
  }

  private func startScanning() {
    guard central.state == .poweredOn else {
      record("Waiting for Bluetooth")
      return
    }
    central.scanForPeripherals(
      withServices: [Self.boseService],
      options: [CBCentralManagerScanOptionAllowDuplicatesKey: false]
    )
  }

  private func stopMonitoringConnections() {
    guard forceRequests.isEmpty else { return }
    central.stopScan()
    for peripheral in peripherals.values where peripheral.state != .disconnected {
      central.cancelPeripheralConnection(peripheral)
    }
  }

  private func matchedSpeaker(
    peripheral: CBPeripheral,
    advertisementData: [String: Any]? = nil
  ) -> FamilySpeaker? {
    if let cached = speakers.first(where: {
      defaults.string(forKey: "peripheral_\($0.id)") == peripheral.identifier.uuidString
    }) {
      return cached
    }
    let localName = advertisementData?[CBAdvertisementDataLocalNameKey] as? String
    let candidateNames = [peripheral.name, localName].compactMap { $0 }
    return speakers.first { speaker in
      candidateNames.contains { namesMatch($0, speaker.name) }
    }
  }

  private func namesMatch(_ candidate: String, _ expected: String) -> Bool {
    let normalize: (String) -> String = {
      $0.lowercased()
        .replacingOccurrences(of: "’", with: "'")
        .replacingOccurrences(of: "le-", with: "")
        .trimmingCharacters(in: .whitespacesAndNewlines)
    }
    let lhs = normalize(candidate)
    let rhs = normalize(expected)
    return lhs == rhs || lhs.contains(rhs) || rhs.contains(lhs)
  }

  private func activeAudioRouteMatches(_ speaker: FamilySpeaker) -> Bool {
    AVAudioSession.sharedInstance().currentRoute.outputs.contains { output in
      let bluetooth = output.portType == .bluetoothA2DP
        || output.portType == .bluetoothHFP
        || output.portType == .bluetoothLE
      return bluetooth && namesMatch(output.portName, speaker.name)
    }
  }

  private func send(_ bytes: [UInt8], to peripheral: CBPeripheral) {
    guard let characteristic = controlCharacteristics[peripheral.identifier] else { return }
    let framed = Data([0] + bytes)
    let writeType: CBCharacteristicWriteType =
      characteristic.properties.contains(.write) ? .withResponse : .withoutResponse
    peripheral.writeValue(framed, for: characteristic, type: writeType)
  }

  private func sendHello(to peripheral: CBPeripheral) {
    send([0x00, 0x01, 0x01, 0x00], to: peripheral)
  }

  private func requestBattery(from peripheral: CBPeripheral) {
    send([0x02, 0x02, 0x01, 0x00], to: peripheral)
  }

  private func requestConnectedSources(from peripheral: CBPeripheral) {
    send([0x04, 0x04, 0x01, 0x00], to: peripheral)
  }

  private func requestNextSource(from peripheral: CBPeripheral) {
    let identifier = peripheral.identifier
    guard var addresses = pendingSourceAddresses[identifier], !addresses.isEmpty else {
      finishPendingBattery(from: peripheral)
      return
    }
    let address = addresses.removeFirst()
    pendingSourceAddresses[identifier] = addresses
    send([0x04, 0x05, 0x01, 0x06] + [UInt8](address), to: peripheral)
  }

  private func consume(_ incoming: Data, from peripheral: CBPeripheral) {
    var data = incoming
    if data.first == 0 { data.removeFirst() }
    var buffer = receiveBuffers[peripheral.identifier] ?? Data()
    buffer.append(data)

    while buffer.count >= 4 {
      let payloadSize = Int(buffer[buffer.startIndex + 3])
      let packetSize = 4 + payloadSize
      guard buffer.count >= packetSize else { break }
      let packet = Data(buffer.prefix(packetSize))
      buffer.removeFirst(packetSize)
      handlePacket(packet, from: peripheral)
    }
    receiveBuffers[peripheral.identifier] = buffer
  }

  private func handlePacket(_ packet: Data, from peripheral: CBPeripheral) {
    let bytes = [UInt8](packet)
    guard bytes.count >= 4 else { return }
    let block = bytes[0]
    let function = bytes[1]
    let operation = bytes[2] & 0x0f

    if operation == 0x04 {
      if pendingBatteryLevels[peripheral.identifier] != nil {
        // Connected-source discovery is optional on older firmware. Preserve
        // the already-read battery value and use this device's custom label.
        finishPendingBattery(from: peripheral)
      } else {
        fail(peripheral, message: "The speaker rejected the battery request")
      }
      return
    }
    guard operation == 0x03 else { return }
    if block == 0x00 && function == 0x01 {
      requestBattery(from: peripheral)
    } else if block == 0x02 && function == 0x02 && bytes.count >= 5 {
      pendingBatteryLevels[peripheral.identifier] = Int(bytes[4])
      pendingConnectedSources[peripheral.identifier] = []
      requestConnectedSources(from: peripheral)
    } else if block == 0x04 && function == 0x04 {
      let payload = Array(bytes.dropFirst(4))
      var addresses: [Data] = []
      if payload.count >= 7 {
        var offset = 1
        while offset + 6 <= payload.count {
          addresses.append(Data(payload[offset..<(offset + 6)]))
          offset += 6
        }
      }
      pendingSourceAddresses[peripheral.identifier] = addresses
      requestNextSource(from: peripheral)
    } else if block == 0x04 && function == 0x05 {
      let payload = Array(bytes.dropFirst(4))
      if let source = parseConnectedSource(payload) {
        var sources = pendingConnectedSources[peripheral.identifier] ?? []
        if !sources.contains(where: {
          $0.name.caseInsensitiveCompare(source.name) == .orderedSame
        }) {
          sources.append(source)
        }
        pendingConnectedSources[peripheral.identifier] = sources
        if sources.count >= 2 {
          finishPendingBattery(from: peripheral)
          return
        }
      }
      requestNextSource(from: peripheral)
    }
  }

  private func parseConnectedSource(_ payload: [UInt8]) -> BoseConnectedSource? {
    // Six address bytes, status, two reserved bytes, then a UTF-8 name.
    guard payload.count >= 10 else { return nil }
    let status = payload[6]
    guard status == 0x01 || status == 0x03 else { return nil }
    let nullAndWhitespace = CharacterSet.whitespacesAndNewlines.union(
      CharacterSet(charactersIn: "\0")
    )
    let name = String(bytes: payload.dropFirst(9), encoding: .utf8)?
      .trimmingCharacters(in: nullAndWhitespace) ?? ""
    guard !name.isEmpty else { return nil }
    return BoseConnectedSource(name: name, isCurrentDevice: status == 0x03)
  }

  private func finishPendingBattery(from peripheral: CBPeripheral) {
    let identifier = peripheral.identifier
    guard let level = pendingBatteryLevels.removeValue(forKey: identifier) else { return }
    let sources = pendingConnectedSources.removeValue(forKey: identifier) ?? []
    pendingSourceAddresses.removeValue(forKey: identifier)
    finishBattery(level, connectedSources: sources, from: peripheral)
  }

  private func finishBattery(
    _ level: Int,
    connectedSources: [BoseConnectedSource],
    from peripheral: CBPeripheral
  ) {
    guard let id = speakerIDsByPeripheral[peripheral.identifier],
          let speaker = speaker(id: id) else { return }
    let forced = forceRequests.remove(speaker.id) != nil
    guard activeAudioRouteMatches(speaker) else {
      record("\(speaker.name) was found, but it is not the active audio output")
      completeAnnouncement(for: speaker)
      finishEphemeral(peripheral)
      return
    }
    let lastKey = "last_announcement_\(speaker.id)"
    let last = defaults.double(forKey: lastKey)
    guard forced || Date().timeIntervalSince1970 - last >= Self.cooldown else {
      completeAnnouncement(for: speaker)
      return
    }
    if !forced && !shouldAnnounceAutomatically(connectedSources) {
      let announcer = automaticAnnouncer(connectedSources)?.name ?? "the other connected device"
      record("\(announcer) will announce both connected devices")
      completeAnnouncement(for: speaker)
      finishEphemeral(peripheral)
      return
    }

    do {
      let session = AVAudioSession.sharedInstance()
      // The playback category routes to A2DP automatically. Avoid requesting
      // an input-oriented Bluetooth option, which could select the call path.
      try session.setCategory(.playback, mode: .spokenAudio, options: [.duckOthers])
      try session.setActive(true)
    } catch {
      record("Battery \(level)%, but iOS could not start speech")
      completeAnnouncement(for: speaker)
      finishEphemeral(peripheral)
      return
    }
    guard activeAudioRouteMatches(speaker) else {
      record("Battery \(level)%, but \(speaker.name) stopped being the audio output")
      completeAnnouncement(for: speaker)
      finishEphemeral(peripheral)
      return
    }

    let connectedDevices = connectedDevicesPhrase(connectedSources)
    let utterance = AVSpeechUtterance(
      string: renderedSpeech(
        for: speaker,
        level: level,
        connectedSources: connectedSources
      )
    )
    utterance.voice = AVSpeechSynthesisVoice(language: "en-US")
    utterance.rate = AVSpeechUtteranceDefaultSpeechRate
    utterance.volume = 1.0
    utteranceSpeakerIDs[ObjectIdentifier(utterance)] = speaker.id
    synthesizer.speak(utterance)
    defaults.set(Date().timeIntervalSince1970, forKey: lastKey)
    record("\(speaker.name): announcing \(level)% for \(connectedDevices)")
  }

  private func finishEphemeral(_ peripheral: CBPeripheral) {
    if !defaults.bool(forKey: Self.monitoringKey) && forceRequests.isEmpty {
      central.stopScan()
      central.cancelPeripheralConnection(peripheral)
    }
  }

  private func fail(_ peripheral: CBPeripheral, message: String) {
    pendingBatteryLevels.removeValue(forKey: peripheral.identifier)
    pendingSourceAddresses.removeValue(forKey: peripheral.identifier)
    pendingConnectedSources.removeValue(forKey: peripheral.identifier)
    if let id = speakerIDsByPeripheral[peripheral.identifier] {
      forceRequests.remove(id)
      if let speaker = speaker(id: id) { completeAnnouncement(for: speaker) }
    }
    record(message)
    finishEphemeral(peripheral)
  }

  private func record(_ message: String) {
    defaults.set(message, forKey: Self.lastEventKey)
  }

  private func authorizationDescription() -> String {
    switch CBManager.authorization {
    case .allowedAlways: return "allowed"
    case .denied: return "denied"
    case .restricted: return "restricted"
    case .notDetermined: return "not determined"
    @unknown default: return "unknown"
    }
  }
}

extension BoseBatteryCoordinator: CBCentralManagerDelegate {
  func centralManagerDidUpdateState(_ central: CBCentralManager) {
    if central.state == .poweredOn,
       defaults.bool(forKey: Self.monitoringKey) || !forceRequests.isEmpty {
      startScanning()
    } else if central.state != .unknown && central.state != .resetting {
      record("Bluetooth is \(central.state == .poweredOff ? "off" : "unavailable")")
    }
  }

  func centralManager(
    _ central: CBCentralManager,
    didDiscover peripheral: CBPeripheral,
    advertisementData: [String: Any],
    rssi RSSI: NSNumber
  ) {
    guard let speaker = matchedSpeaker(
      peripheral: peripheral,
      advertisementData: advertisementData
    ), shouldUse(speaker) else { return }
    if announcementsInFlight.contains(speaker.id),
       speakerIDsByPeripheral[peripheral.identifier] == speaker.id {
      return
    }
    announcementsInFlight.insert(speaker.id)
    defaults.set(peripheral.identifier.uuidString, forKey: "peripheral_\(speaker.id)")
    peripherals[peripheral.identifier] = peripheral
    speakerIDsByPeripheral[peripheral.identifier] = speaker.id
    peripheral.delegate = self
    if peripheral.state == .disconnected {
      central.connect(peripheral, options: [CBConnectPeripheralOptionNotifyOnDisconnectionKey: true])
    } else if peripheral.state == .connected {
      receiveBuffers[peripheral.identifier] = Data()
      peripheral.discoverServices([Self.boseService])
    }
  }

  func centralManager(_ central: CBCentralManager, didConnect peripheral: CBPeripheral) {
    peripheral.delegate = self
    receiveBuffers[peripheral.identifier] = Data()
    peripheral.discoverServices([Self.boseService])
  }

  func centralManager(
    _ central: CBCentralManager,
    didFailToConnect peripheral: CBPeripheral,
    error: Error?
  ) {
    fail(peripheral, message: "Could not connect to the Bose battery service")
  }

  func centralManager(
    _ central: CBCentralManager,
    didDisconnectPeripheral peripheral: CBPeripheral,
    error: Error?
  ) {
    controlCharacteristics.removeValue(forKey: peripheral.identifier)
    receiveBuffers.removeValue(forKey: peripheral.identifier)
    pendingBatteryLevels.removeValue(forKey: peripheral.identifier)
    pendingSourceAddresses.removeValue(forKey: peripheral.identifier)
    pendingConnectedSources.removeValue(forKey: peripheral.identifier)
    if let id = speakerIDsByPeripheral[peripheral.identifier],
       let speaker = speaker(id: id), shouldUse(speaker) {
      completeAnnouncement(for: speaker)
      startScanning()
    }
  }

  func centralManager(_ central: CBCentralManager, willRestoreState dict: [String: Any]) {
    guard let restored = dict[CBCentralManagerRestoredStatePeripheralsKey] as? [CBPeripheral] else {
      return
    }
    for peripheral in restored {
      guard let speaker = matchedSpeaker(peripheral: peripheral), shouldUse(speaker) else { continue }
      peripherals[peripheral.identifier] = peripheral
      speakerIDsByPeripheral[peripheral.identifier] = speaker.id
      peripheral.delegate = self
      if peripheral.state == .connected {
        peripheral.discoverServices([Self.boseService])
      }
    }
  }
}

extension BoseBatteryCoordinator: CBPeripheralDelegate {
  func peripheral(_ peripheral: CBPeripheral, didDiscoverServices error: Error?) {
    guard error == nil,
          let service = peripheral.services?.first(where: { $0.uuid == Self.boseService }) else {
      fail(peripheral, message: "The Bose battery service was not available")
      return
    }
    peripheral.discoverCharacteristics(
      [Self.secureCharacteristic, Self.fallbackCharacteristic],
      for: service
    )
  }

  func peripheral(
    _ peripheral: CBPeripheral,
    didDiscoverCharacteristicsFor service: CBService,
    error: Error?
  ) {
    guard error == nil, let characteristics = service.characteristics else {
      fail(peripheral, message: "The Bose battery control was not available")
      return
    }
    let characteristic = characteristics.first { $0.uuid == Self.secureCharacteristic }
      ?? characteristics.first { $0.uuid == Self.fallbackCharacteristic }
    guard let characteristic else {
      fail(peripheral, message: "This Bose firmware did not expose battery control")
      return
    }
    controlCharacteristics[peripheral.identifier] = characteristic
    peripheral.setNotifyValue(true, for: characteristic)
  }

  func peripheral(
    _ peripheral: CBPeripheral,
    didUpdateNotificationStateFor characteristic: CBCharacteristic,
    error: Error?
  ) {
    guard error == nil, characteristic.isNotifying else {
      fail(peripheral, message: "Could not listen for the Bose battery response")
      return
    }
    sendHello(to: peripheral)
  }

  func peripheral(
    _ peripheral: CBPeripheral,
    didUpdateValueFor characteristic: CBCharacteristic,
    error: Error?
  ) {
    guard error == nil, let value = characteristic.value else {
      fail(peripheral, message: "The Bose battery response could not be read")
      return
    }
    consume(value, from: peripheral)
  }
}

extension BoseBatteryCoordinator: AVSpeechSynthesizerDelegate {
  func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didFinish utterance: AVSpeechUtterance) {
    finishSpeech(utterance)
  }

  func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didCancel utterance: AVSpeechUtterance) {
    finishSpeech(utterance)
  }

  private func finishSpeech(_ utterance: AVSpeechUtterance) {
    if let id = utteranceSpeakerIDs.removeValue(forKey: ObjectIdentifier(utterance)),
       let speaker = speaker(id: id) {
      completeAnnouncement(for: speaker)
    }
    // AVSpeechSynthesizer has finished submitting samples before a Bluetooth
    // speaker necessarily finishes playing them. Leave the route active long
    // enough for its buffer to drain so the last word is not clipped.
    DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) { [weak self] in
      guard let self else { return }
      if !self.defaults.bool(forKey: Self.monitoringKey) && self.forceRequests.isEmpty {
        self.stopMonitoringConnections()
      }
      try? AVAudioSession.sharedInstance().setActive(
        false,
        options: .notifyOthersOnDeactivation
      )
    }
  }
}

@main
@objc class AppDelegate: FlutterAppDelegate, FlutterImplicitEngineDelegate {
  private let batteryCoordinator = BoseBatteryCoordinator()

  override func application(
    _ application: UIApplication,
    didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?
  ) -> Bool {
    return super.application(application, didFinishLaunchingWithOptions: launchOptions)
  }

  func didInitializeImplicitFlutterEngine(_ engineBridge: FlutterImplicitEngineBridge) {
    GeneratedPluginRegistrant.register(with: engineBridge.pluginRegistry)
    batteryCoordinator.register(messenger: engineBridge.applicationRegistrar.messenger())
  }
}
