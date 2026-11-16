import unittest
from datetime import datetime, timedelta, timezone

from nudgy import discover
from nudgy.model import Item, important, score

TZ = timezone(timedelta(hours=1))
NOW = datetime(2026, 11, 11, 12, 0, tzinfo=TZ)

ICS = """BEGIN:VCALENDAR
BEGIN:VEVENT
DTSTART:20261128T090000Z
DTEND:20261129T170000Z
SUMMARY:BIG HARBOUR HACK
DESCRIPTION:Co-hosted with Google DeepMind. €10k prize pool.
LOCATION:https://luma.com/event/evt-1
GEO:53.56;10.01
URL:https://luma.com/bbh
END:VEVENT
BEGIN:VEVENT
DTSTART:20261121T090000Z
SUMMARY:Gemini NYC Hackathon
LOCATION:https://example.com
GEO:40.71;-74.00
END:VEVENT
BEGIN:VEVENT
DTSTART;VALUE=DATE:20261127
DTEND;VALUE=DATE:20261130
SUMMARY:Defense Tech Hackathon - Paris
GEO:48.85;2.35
END:VEVENT
BEGIN:VEVENT
DTSTART:20261114T100000Z
SUMMARY:Sunday Run Club Hamburg
GEO:53.55;9.99
END:VEVENT
END:VCALENDAR
"""


def at_home(city="Hamburg"):
    from unittest import mock
    return mock.patch.object(discover, "home", lambda: city)


class NoHome(unittest.TestCase):
    def test_without_a_home_city_nothing_is_home(self):
        with at_home(""):
            self.assertEqual(discover.place_from_geo(53.55, 9.99), ("Europe", "Hamburg"))
            self.assertEqual(discover.region("Hamburg"), "Europe")


class Places(unittest.TestCase):
    def setUp(self):
        patcher = at_home()
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_coordinates(self):
        self.assertEqual(discover.place_from_geo(53.55, 9.99), ("Home", "Hamburg"))
        self.assertEqual(discover.place_from_geo(50.94, 6.96), ("Near", "Cologne"))       # 360 km away
        self.assertEqual(discover.place_from_geo(48.14, 11.58), ("Europe", "Munich"))     # 610 km away
        self.assertEqual(discover.place_from_geo(48.86, 2.35), ("Europe", "Paris"))
        self.assertIsNone(discover.place_from_geo(40.71, -74.0))

    def test_text(self):
        self.assertEqual(discover.region("Example GmbH, Hamburg, Germany"), "Home")
        self.assertEqual(discover.region("Example GmbH, Cologne, Germany"), "Near")
        self.assertEqual(discover.region("Example GmbH, Munich, Germany"), "Europe")
        self.assertEqual(discover.region("Volkshaus Zürich"), "Europe")
        self.assertIsNone(discover.region("Austin, TX"))


def with_ml_ai_interests(test):
    """Uses examples/discover-ml-ai.json's topics and big names (the defaults have none)."""
    import json
    from pathlib import Path
    from unittest import mock
    example = json.loads((Path(__file__).parent.parent / "examples" / "discover-ml-ai.json").read_text())
    settings = {**discover.config(), "topics": example["topics"], "big_names": example["big_names"]}
    patcher = mock.patch.object(discover, "config", lambda: settings)
    patcher.start()
    test.addCleanup(patcher.stop)


class Scoring(unittest.TestCase):
    def setUp(self):
        with_ml_ai_interests(self)

    def test_prize(self):
        self.assertEqual(discover.biggest_prize("prize pool of €10,000"), 10000)
        self.assertEqual(discover.biggest_prize("CHF 50k in prizes"), 50000)
        self.assertEqual(discover.biggest_prize("$325K in prizes"), 325000)

    def test_big_hackathon_at_home_beats_a_talk_abroad(self):
        with at_home():
            hack, reasons = discover.fit("AI Hackathon", "Google DeepMind, €20k prizes", "Home")
        self.assertEqual(reasons[0], "Hamburg")
        talk, _ = discover.fit("Evening talk", "about AI", "Europe")
        self.assertGreater(hack, 70)
        self.assertLess(talk, discover.MIN_FIT)


class Feed(unittest.TestCase):
    def setUp(self):
        with_ml_ai_interests(self)

    def test_ics(self):
        with at_home():
            items = discover.from_ics(ICS, NOW, "Google DeepMind")
        titles = [i.title for i in items]
        self.assertIn("BIG HARBOUR HACK", titles)
        self.assertIn("Defense Tech Hackathon - Paris", titles)          # all-day events count
        self.assertNotIn("Gemini NYC Hackathon", titles)                 # outside Europe
        self.assertNotIn("Sunday Run Club Hamburg", titles)
        big = items[titles.index("BIG HARBOUR HACK")]
        self.assertEqual(big.url, "https://luma.com/bbh")
        self.assertIn("Hamburg", big.detail)
        self.assertIn("prize 10k", big.detail)

    def test_known_events_are_dropped_and_top_reassigned(self):
        found = [Item(f"potential:{n}", "potential", f"Event {n}", due=NOW.isoformat(), url=f"https://luma.com/{n}",
                      tags=["event", "fit:80"] + (["top"] if n < discover.TOP else [])) for n in range(10)]
        mine = [Item("luma:x", "luma", "Event 0", due=NOW.isoformat())]
        left = discover.drop_known(found, mine)
        self.assertEqual(len(left), 9)
        self.assertEqual(sum("top" in i.tags for i in left), discover.TOP)

    def test_only_top_ideas_are_important(self):
        top = Item("p1", "potential", "A", tags=["fit:90", "top"])
        other = Item("p2", "potential", "B", tags=["fit:60"])
        self.assertTrue(important(top, score(top, NOW)))
        self.assertFalse(important(other, score(other, NOW)))
        self.assertGreater(score(top, NOW), score(other, NOW))


class OptIn(unittest.TestCase):
    def test_off_until_turned_on(self):
        import os
        import tempfile
        from unittest import mock
        from datetime import datetime, timezone
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": folder, "XDG_DATA_HOME": folder}):
            self.assertFalse(discover.config()["enabled"])
            with mock.patch.object(discover, "collect", side_effect=AssertionError("fetched while off")):
                self.assertEqual(discover.fetch(datetime.now(timezone.utc)), ([], []))
            from nudgy import settings
            settings.update("discover", {"enabled": True, "rss": []})
            self.assertTrue(discover.config()["enabled"])
