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

private final class BoseBatteryCoordinator: NSObject {
  private static let channelName = "com.liamapp.bose_battery_voice/control"
  private static let monitoringKey = "monitoring"
  private static let lastEventKey = "last_event"
  private static let speechTemplateKey = "speech_template"
  private static let deviceLabelKey = "device_label"
  private static let defaultSpeechTemplate = "Battery {battery} percent."
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
  private var forceRequests = Set<String>()
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
      forceRequests.insert(id)
      startScanning()
      record("Looking for \(speaker(id: id)!.name)")
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
    return value?.isEmpty == false ? value! : Self.defaultSpeechTemplate
  }

  private var deviceLabel: String {
    let value = defaults.string(forKey: Self.deviceLabelKey)?
      .trimmingCharacters(in: .whitespacesAndNewlines)
    return value?.isEmpty == false ? value! : UIDevice.current.model
  }

  private func renderedSpeech(for speaker: FamilySpeaker, level: Int) -> String {
    speechTemplate
      .replacingOccurrences(of: "{speaker}", with: speaker.name)
      .replacingOccurrences(of: "{battery}", with: String(max(0, min(level, 100))))
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
      fail(peripheral, message: "The speaker rejected the battery request")
      return
    }
    guard operation == 0x03 else { return }
    if block == 0x00 && function == 0x01 {
      requestBattery(from: peripheral)
    } else if block == 0x02 && function == 0x02 && bytes.count >= 5 {
      finishBattery(Int(bytes[4]), from: peripheral)
    }
  }

  private func finishBattery(_ level: Int, from peripheral: CBPeripheral) {
    guard let id = speakerIDsByPeripheral[peripheral.identifier],
          let speaker = speaker(id: id) else { return }
    let forced = forceRequests.remove(speaker.id) != nil
    guard activeAudioRouteMatches(speaker) else {
      record("\(speaker.name) was found, but it is not the active audio output")
      finishEphemeral(peripheral)
      return
    }
    let lastKey = "last_announcement_\(speaker.id)"
    let last = defaults.double(forKey: lastKey)
    guard forced || Date().timeIntervalSince1970 - last >= Self.cooldown else { return }

    do {
      let session = AVAudioSession.sharedInstance()
      // The playback category routes to A2DP automatically. Avoid requesting
      // an input-oriented Bluetooth option, which could select the call path.
      try session.setCategory(.playback, mode: .spokenAudio, options: [.duckOthers])
      try session.setActive(true)
    } catch {
      record("Battery \(level)%, but iOS could not start speech")
      finishEphemeral(peripheral)
      return
    }
    guard activeAudioRouteMatches(speaker) else {
      record("Battery \(level)%, but \(speaker.name) stopped being the audio output")
      finishEphemeral(peripheral)
      return
    }

    let utterance = AVSpeechUtterance(string: renderedSpeech(for: speaker, level: level))
    utterance.voice = AVSpeechSynthesisVoice(language: "en-US")
    utterance.rate = AVSpeechUtteranceDefaultSpeechRate
    utteranceSpeakerIDs[ObjectIdentifier(utterance)] = speaker.id
    synthesizer.speak(utterance)
    defaults.set(Date().timeIntervalSince1970, forKey: lastKey)
    record("\(speaker.name): announcing \(level)%")
  }

  private func finishEphemeral(_ peripheral: CBPeripheral) {
    if !defaults.bool(forKey: Self.monitoringKey) && forceRequests.isEmpty {
      central.stopScan()
      central.cancelPeripheralConnection(peripheral)
    }
  }

  private func fail(_ peripheral: CBPeripheral, message: String) {
    if let id = speakerIDsByPeripheral[peripheral.identifier] {
      forceRequests.remove(id)
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
    defaults.set(peripheral.identifier.uuidString, forKey: "peripheral_\(speaker.id)")
    peripherals[peripheral.identifier] = peripheral
    speakerIDsByPeripheral[peripheral.identifier] = speaker.id
    peripheral.delegate = self
    if peripheral.state == .disconnected {
      central.connect(peripheral, options: [CBConnectPeripheralOptionNotifyOnDisconnectionKey: true])
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
    if let id = speakerIDsByPeripheral[peripheral.identifier],
       let speaker = speaker(id: id), shouldUse(speaker) {
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
    utteranceSpeakerIDs.removeValue(forKey: ObjectIdentifier(utterance))
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
