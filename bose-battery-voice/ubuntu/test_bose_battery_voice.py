import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


MODULE_PATH = Path(__file__).with_name("bose_battery_voice.py")
SPEC = importlib.util.spec_from_file_location("bose_battery_voice", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class DesktopBatteryVoiceTests(unittest.TestCase):
    def test_connected_parser(self):
        def runner(_command):
            return "Device BC:87:FA:2E:7C:63\n\tConnected: yes\n"

        self.assertTrue(MODULE.bluetooth_connected(MODULE.ELIZABETH, runner))

    def test_disconnected_parser(self):
        def runner(_command):
            return "Device BC:87:FA:2E:7C:63\n\tConnected: no\n"

        self.assertFalse(MODULE.bluetooth_connected(MODULE.ELIZABETH, runner))

    def test_sink_matches_mac(self):
        sink = "bluez_output.BC_87_FA_2E_7C_63.1"
        self.assertTrue(MODULE.sink_matches(sink, MODULE.ELIZABETH))
        self.assertFalse(MODULE.sink_matches(sink, MODULE.FREDDIE))

    def test_selected_speakers_default_shape(self):
        self.assertEqual(MODULE.selected_speakers("elizabeth"), (MODULE.ELIZABETH,))
        self.assertEqual(
            MODULE.selected_speakers("both"),
            (MODULE.ELIZABETH, MODULE.FREDDIE),
        )

    def test_connected_devices_phrase_names_both_multipoint_sources(self):
        sources = (
            SimpleNamespace(name="Workstation", is_current_device=True),
            SimpleNamespace(name="Ron's phone", is_current_device=False),
        )

        self.assertEqual(
            MODULE.connected_devices_phrase(sources, MODULE.DesktopSettings()),
            "Ubuntu desktop and Ron's phone",
        )

    def test_only_elected_multipoint_helper_announces_automatically(self):
        ubuntu_view = (
            SimpleNamespace(name="Alien3-Ubuntu", is_current_device=True),
            SimpleNamespace(name="Ron's S26 Ultra", is_current_device=False),
        )
        phone_view = (
            SimpleNamespace(name="Alien3-Ubuntu", is_current_device=False),
            SimpleNamespace(name="Ron's S26 Ultra", is_current_device=True),
        )

        self.assertFalse(MODULE.should_announce_automatically(ubuntu_view))
        self.assertTrue(MODULE.should_announce_automatically(phone_view))

    def test_settings_round_trip(self):
        settings = MODULE.DesktopSettings(
            device_label="Alien desktop",
            speech_template="{devices}: {battery}%",
            announcement_volume_percent=82,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.ini"
            MODULE.save_settings(settings, path)

            self.assertEqual(MODULE.load_settings(path), settings)

    def test_speech_temporarily_raises_and_restores_desktop_volume(self):
        commands = []

        def runner(command, **_kwargs):
            commands.append(command)
            if command[:2] == ["pactl", "get-sink-volume"]:
                return "Volume: front-left: 13107 / 20% / -41.94 dB"
            return ""

        MODULE.speak(
            80,
            (),
            runner,
            MODULE.DesktopSettings(announcement_volume_percent=75),
        )

        self.assertEqual(commands[1][-1], "75%")
        self.assertIn("Ubuntu desktop connected", commands[2][-1])
        self.assertEqual(commands[3][-1], "20%")


if __name__ == "__main__":
    unittest.main()
