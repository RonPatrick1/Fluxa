import 'package:bose_battery_voice/main.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  const channel = MethodChannel('com.liamapp.bose_battery_voice/control');

  setUp(() {
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(channel, (call) async {
          if (call.method == 'getStatus') {
            return <String, dynamic>{
              'platform': 'Test',
              'monitoring': false,
              'elizabethEnabled': true,
              'freddieEnabled': false,
              'lastEvent': '',
            };
          }
          return true;
        });
  });

  tearDown(() {
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(channel, null);
  });

  testWidgets('shows both family speakers and safe defaults', (tester) async {
    await tester.pumpWidget(const BatteryVoiceApp());
    await tester.pumpAndSettle();

    expect(find.text("Elizabeth's Bose"), findsOneWidget);
    await tester.scrollUntilVisible(
      find.text("Freddie's Bose"),
      240,
      scrollable: find.byType(Scrollable).first,
    );
    expect(find.text("Freddie's Bose"), findsOneWidget);
    await tester.scrollUntilVisible(
      find.textContaining('never contacts Bose'),
      240,
      scrollable: find.byType(Scrollable).first,
    );
    expect(find.textContaining('never contacts Bose'), findsOneWidget);
  });

  testWidgets('saves a custom device name without a lifecycle error', (
    tester,
  ) async {
    await tester.pumpWidget(const BatteryVoiceApp());
    await tester.pumpAndSettle();

    await tester.tap(find.text('Customize'));
    await tester.pumpAndSettle();
    await tester.enterText(find.byType(TextField).first, "Ron's phone");
    await tester.tap(find.text('Save'));
    await tester.pumpAndSettle();

    expect(tester.takeException(), isNull);
    expect(find.text('Customize announcement'), findsNothing);
  });

  testWidgets('offers a custom announcement test button', (tester) async {
    await tester.pumpWidget(const BatteryVoiceApp());
    await tester.pumpAndSettle();

    expect(find.text('Test custom announcement'), findsOneWidget);
    await tester.tap(find.text('Test custom announcement'));
    await tester.pump(const Duration(seconds: 2));
    await tester.pump();

    expect(tester.takeException(), isNull);
  });
}
