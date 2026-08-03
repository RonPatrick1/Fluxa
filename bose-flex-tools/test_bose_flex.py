import unittest

from bose_flex import BoseError, VoicePromptConfig


class VoicePromptConfigTests(unittest.TestCase):
    def test_freddie_reports_battery_prompts_enabled(self):
        config = VoicePromptConfig.from_payload(bytes.fromhex("e10001815e0101"))

        self.assertTrue(config.enabled)
        self.assertEqual(config.language, 1)
        self.assertEqual(config.supported_languages, 0x0001815E)
        self.assertTrue(config.default_language)
        self.assertTrue(config.user_can_toggle)
        self.assertTrue(config.battery_level_supported)
        self.assertTrue(config.battery_level_enabled)

    def test_elizabeth_reports_battery_prompts_unsupported(self):
        config = VoicePromptConfig.from_payload(bytes.fromhex("41000081020000"))

        self.assertFalse(config.enabled)
        self.assertEqual(config.language, 1)
        self.assertEqual(config.supported_languages, 0x00008102)
        self.assertTrue(config.default_language)
        self.assertFalse(config.user_can_toggle)
        self.assertFalse(config.battery_level_supported)
        self.assertFalse(config.battery_level_enabled)

    def test_old_five_byte_response_has_no_battery_option(self):
        config = VoicePromptConfig.from_payload(bytes.fromhex("e10001815e"))

        self.assertFalse(config.battery_level_supported)
        self.assertFalse(config.battery_level_enabled)

    def test_rejects_unknown_response_size(self):
        with self.assertRaises(BoseError):
            VoicePromptConfig.from_payload(b"\x00")


if __name__ == "__main__":
    unittest.main()
