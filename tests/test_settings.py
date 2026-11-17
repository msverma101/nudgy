import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nudgy import settings


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        patcher = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.folder.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.folder.cleanup)
        self.config = Path(self.folder.name) / "nudgy"

    def test_defaults_are_neutral(self):
        loaded = settings.load()
        self.assertEqual(loaded["claude_notes"]["folder"], "")
        self.assertEqual(loaded["discover"]["home"], "")
        self.assertEqual(loaded["discover"]["topics"], [])
        self.assertEqual(loaded["google"]["skip_calendars"], [])
        self.assertFalse(any(word in loaded["ranking"]["urgent_words"] for word in ("interview", "job offer")))
        for section in ("focus", "voice", "digest", "discover"):
            self.assertFalse(loaded[section]["enabled"], section)

    def test_old_files_are_migrated_once(self):
        self.config.mkdir(parents=True)
        (self.config / "focus.json").write_text(json.dumps({"enabled": True, "work_topics": ["robots"]}))
        (self.config / "discover.json").write_text(json.dumps({"home": "Hamburg"}))
        (self.config / "google.json").write_text(json.dumps({"skip_calendars": ["Alarms"]}))
        loaded = settings.load()
        self.assertTrue(loaded["focus"]["enabled"])
        self.assertEqual(loaded["focus"]["work_topics"], ["robots"])
        self.assertTrue(loaded["discover"]["enabled"])            # having discover.json meant "on"
        self.assertEqual(loaded["discover"]["home"], "Hamburg")
        self.assertEqual(loaded["google"]["skip_calendars"], ["Alarms"])
        self.assertFalse((self.config / "focus.json").exists())
        self.assertTrue((self.config / "focus.json.migrated").exists())
        self.assertFalse(settings.migrate())                      # only once

    def test_update_keeps_other_sections_and_is_private(self):
        settings.update("voice", {"enabled": True})
        settings.update("phone", {"folder": "~/Sync"})
        saved = json.loads(settings.path().read_text())
        self.assertEqual(saved, {"voice": {"enabled": True}, "phone": {"folder": "~/Sync"}})
        self.assertEqual(settings.path().stat().st_mode & 0o777, 0o600)
        self.assertEqual(settings.section("voice")["whisper"], "base")         # defaults still filled in

    def test_wrong_types_fall_back_to_defaults(self):
        settings.update("focus", {"enabled": "yes", "interval_seconds": True, "unsure_below": 1})
        with self.assertLogs("nudgy.settings", "WARNING"):
            focus = settings.section("focus")
        self.assertFalse(focus["enabled"])
        self.assertEqual(focus["interval_seconds"], 3600)
        self.assertEqual(focus["unsure_below"], 1)                # a whole number is fine for a decimal

    def test_broken_file_uses_defaults(self):
        self.config.mkdir(parents=True)
        settings.path().write_text("{ not json")
        with self.assertLogs("nudgy.settings", "WARNING"):
            self.assertFalse(settings.section("voice")["enabled"])

    def test_added_voice_keeps_the_others(self):
        settings.update("voice", {"voices": {"fr": "fr_FR-siwis-medium"}})
        self.assertEqual(set(settings.section("voice")["voices"]), {"en", "de", "fr"})


if __name__ == "__main__":
    unittest.main()
