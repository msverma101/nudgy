import unittest
from datetime import datetime, timezone
from unittest import mock

from nudgy import sources
from nudgy.model import Item
from nudgy.sources.base import Round, Source

T = datetime(2026, 11, 17, 9, 0, tzinfo=timezone.utc)


class PluginTest(unittest.TestCase):
    def test_every_plugin_follows_the_pattern(self):
        found = sources.plugins()
        self.assertEqual([s.group for s in found], list(sources.GROUPS))
        self.assertEqual(len(set(sources.GROUPS)), len(found))                  # one group each
        self.assertLess(sources.GROUPS.index("google"), sources.GROUPS.index("events"))   # Calendar first
        for source in found:
            self.assertIsInstance(source, Source)
            self.assertIsInstance(source.status(), dict)

    def test_accounts_lines(self):
        self.assertTrue({"anthropic", "google", "linkedin", "luma", "keep", "signal"} <= set(sources.status()))

    def test_one_broken_source_does_not_stop_the_others(self):
        class Broken(Source):
            group, order = "broken", 1

            def fetch(self, round):
                raise OSError("no network")

        class Fine(Source):
            group, order = "fine", 2

            def fetch(self, round):
                return {"fine": [Item(id="fine:1", source="fine", title="ok")]}

        with mock.patch.object(sources, "plugins", return_value=[Broken(), Fine()]):
            items, errors = sources.sync_groups(T, {"broken", "fine"})
        self.assertEqual(list(items), ["fine"])
        self.assertIn("broken", errors)

    def test_round_shares_calendar(self):
        round = Round(T, calendar=[Item(id="calendar:old", source="calendar", title="old")])
        self.assertEqual(round.known_calendar()[0].id, "calendar:old")
        round.items["calendar"] = []
        self.assertEqual(round.known_calendar(), [])                           # this round's wins


if __name__ == "__main__":
    unittest.main()
