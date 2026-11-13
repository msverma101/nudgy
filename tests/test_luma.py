import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from nudgy import luma, sources, google, chats
from nudgy.model import NOW, SOON, Item, bucket, friendly_time, score

CET = timezone(timedelta(hours=1))
T = datetime(2026, 11, 11, 12, 0, tzinfo=CET)

FEED = """BEGIN:VCALENDAR\r
VERSION:2.0\r
BEGIN:VEVENT\r
UID:evt-robotics@lu.ma\r
SUMMARY:Harbour Robotics & AI Hackathon\\, Day 1\r
DTSTART:20261121T080000Z\r
DTEND:20261121T200000Z\r
LOCATION:Example Hall\\, Main Street 1\r
URL:https://luma.com/robotics-hack\r
DESCRIPTION:Bring a laptop.\\nSee https://luma.com/robotics-hack\r
 for details\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:evt-bbh@lu.ma\r
SUMMARY:BIG HARBOUR HACK\r
DTSTART;TZID=Europe/Amsterdam:20261111T123000\r
DTEND;TZID=Europe/Amsterdam:20261111T140000\r
STATUS:TENTATIVE\r
LOCATION:https://luma.com/bbh\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:evt-past@lu.ma\r
SUMMARY:Old meetup\r
DTSTART:20261101T180000Z\r
DTEND:20261101T200000Z\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:evt-far@lu.ma\r
SUMMARY:Next year\r
DTSTART:20270301T180000Z\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:evt-cancel@lu.ma\r
SUMMARY:Cancelled thing\r
STATUS:CANCELLED\r
DTSTART:20261112T180000Z\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:evt-allday@lu.ma\r
SUMMARY:Two-day summit\r
DTSTART;VALUE=DATE:20261111\r
DTEND;VALUE=DATE:20261113\r
END:VEVENT\r
END:VCALENDAR\r
"""


class Parsing(unittest.TestCase):
    def setUp(self):
        self.items = {i.title: i for i in luma.luma_items(FEED, T)}

    def test_keeps_upcoming_and_current_only(self):
        self.assertEqual(sorted(self.items), ["BIG HARBOUR HACK", "Harbour Robotics & AI Hackathon, Day 1",
                                              "Two-day summit"])

    def test_fields(self):
        hack = self.items["Harbour Robotics & AI Hackathon, Day 1"]
        self.assertEqual(hack.due, "2026-11-21T09:00:00+01:00")
        self.assertEqual(hack.url, "https://luma.com/robotics-hack")
        self.assertEqual(hack.detail, "Example Hall")
        self.assertEqual(hack.id, "luma:evt-robotics@lu.ma")

    def test_waitlisted_event_happening_now(self):
        bbh = self.items["BIG HARBOUR HACK"]
        self.assertEqual(bbh.detail, "Pending or waitlisted")
        self.assertEqual(bbh.url, "https://luma.com/bbh")   # no URL field: taken from LOCATION
        self.assertEqual(bucket(score(bbh, T + timedelta(minutes=45))), NOW)
        self.assertEqual(friendly_time(bbh, T + timedelta(minutes=45)), "Happening now")

    def test_all_day(self):
        summit = self.items["Two-day summit"]
        self.assertTrue(summit.all_day)
        self.assertEqual(bucket(score(summit, T)), SOON)


class Duplicates(unittest.TestCase):
    def test_same_event_in_google_calendar_is_dropped(self):
        events = luma.luma_items(FEED, T)
        calendar = [
            Item("calendar:x", "calendar", "Harbour Robotics & AI Hackathon, Day 1",
                 due="2026-11-21T08:00:00Z"),                                   # same moment, UTC
            Item("calendar:y", "calendar", "BBH (from invite)", detail="https://luma.com/bbh", due="2026-11-11"),
        ]
        left = [i.title for i in luma.drop_duplicates(events, calendar)]
        self.assertEqual(left, ["Two-day summit"])   # one matched by time, one by Luma link


class Feed(unittest.TestCase):
    def test_save_validates_converts_webcal_and_is_private(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": home}):
            self.assertIsNone(luma.fetch_luma(T))                 # not connected: skipped
            with self.assertRaises(ValueError):
                luma.save_feed("not a link")
            luma.save_feed(" webcal://api.lu.ma/ics/get?entity=usr&id=abc ")
            self.assertEqual(luma.feed_url(), "https://api.lu.ma/ics/get?entity=usr&id=abc")
            self.assertEqual(oct(luma.feed_path().stat().st_mode & 0o777), "0o600")
            luma.forget_feed()
            self.assertIsNone(luma.feed_url())

    def test_html_instead_of_calendar_is_an_error(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"<html>login</html>"
        with mock.patch.object(luma, "feed_url", return_value="https://x"), \
                mock.patch.object(luma.urllib.request, "urlopen", return_value=response):
            with self.assertRaises(ValueError):
                luma.fetch_luma(T)

    def test_sync_dedupes_against_calendar(self):
        calendar = [Item("calendar:x", "calendar", "BIG HARBOUR HACK", due="2026-11-11T12:30:00+01:00")]
        with mock.patch.object(google, "sync_all", return_value=({"calendar": calendar}, {})), \
                mock.patch.object(chats, "fetch_signal", return_value=None), \
                mock.patch.object(chats, "fetch_whatsapp", return_value=None), \
                mock.patch.object(sources.claude_notes, "read", return_value=[]), \
                mock.patch.object(luma, "fetch_luma", return_value=luma.luma_items(FEED, T)):
            items, errors = sources.sync_all(T)
        self.assertEqual(errors, {})
        self.assertNotIn("BIG HARBOUR HACK", [i.title for i in items["luma"]])


if __name__ == "__main__":
    unittest.main()
