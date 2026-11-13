import json
import os
import stat
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from nudgy import google

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone(timedelta(hours=1)))
CAL = "https://www.googleapis.com/calendar/v3"
TASKS = "https://tasks.googleapis.com/tasks/v1"
MAIL = "https://gmail.googleapis.com/gmail/v1/users/me"


def message(mid, sender, subject, labels=()):
    return {"id": mid, "labelIds": list(labels), "payload": {"headers": [
        {"name": "From", "value": sender}, {"name": "Subject", "value": subject}]}}


class Fetching(unittest.TestCase):
    def fake(self, routes):
        def api(url, params=None):
            self.calls.append((url, params))
            return routes[url]
        self.calls = []
        patcher = mock.patch.object(google, "api", api)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_calendar_skips_chosen_hidden_and_cancelled(self):
        skip = mock.patch.object(google, "skip_calendars", lambda: {"My Alarms"})
        skip.start()
        self.addCleanup(skip.stop)
        self.fake({
            f"{CAL}/users/me/calendarList": {"items": [
                {"id": "me@x.com", "summary": "Mine", "selected": True},
                {"id": "alarms", "summary": "My Alarms", "selected": True},
                {"id": "hidden", "summary": "Hidden"},
                {"id": "en.german#holiday@group.v.calendar.google.com", "summary": "Holidays in Germany",
                 "selected": True},
                {"id": "addressbook#contacts@group.v.calendar.google.com", "summary": "Birthdays",
                 "selected": True}]},
            f"{CAL}/calendars/me%40x.com/events": {"items": [
                {"id": "1", "summary": "Exam", "start": {"dateTime": "2026-11-06T14:00:00+01:00"},
                 "end": {"dateTime": "2026-11-06T15:00:00+01:00"}, "htmlLink": "https://cal/1"},
                {"id": "2", "status": "cancelled"},
                {"id": "3", "start": {"date": "2026-11-07"}, "end": {"date": "2026-11-08"}}]}})
        items = google.fetch_calendar(NOW)
        self.assertEqual([i.title for i in items], ["Exam", "(no title)"])
        self.assertTrue(items[1].all_day)

    def test_tasks_uses_date_only_and_skips_archive_list(self):
        self.fake({
            f"{TASKS}/users/@me/lists": {"items": [
                {"id": "a", "title": "My Tasks"}, {"id": "b", "title": "Old Google Keep Reminders"}]},
            f"{TASKS}/lists/a/tasks": {"items": [
                {"id": "1", "title": "Pay rent", "due": "2026-11-07T00:00:00.000Z"},
                {"id": "2", "title": "  "}]}})
        items = google.fetch_tasks()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].due, "2026-11-07")

    def test_gmail(self):
        self.fake({
            f"{MAIL}/profile": {"emailAddress": "me@x.com"},
            f"{MAIL}/messages": {"messages": [{"id": "m1"}]},
            f"{MAIL}/messages/m1": message("m1", "Prof X <x@uni.edu>", "Thesis", ["IMPORTANT", "STARRED"])})
        [item] = google.fetch_gmail()
        self.assertEqual(item.detail, "Prof X")
        self.assertEqual(sorted(item.tags), ["important", "starred"])
        self.assertTrue(item.url.startswith("https://mail.google.com/mail/u/me@x.com/#all/m1"))

    def test_linkedin_keeps_people_and_drops_noise(self):
        self.fake({
            f"{MAIL}/profile": {"emailAddress": "me@x.com"},
            f"{MAIL}/messages": {"messages": [{"id": "1"}, {"id": "2"}]},
            f"{MAIL}/messages/1": message("1", "A B <messages-noreply@linkedin.com>", "A sent you a message"),
            f"{MAIL}/messages/2": message("2", "LinkedIn <notifications-noreply@linkedin.com>", "5 viewed you")})
        items = google.fetch_linkedin()
        self.assertEqual([i.tags for i in items], [["message"]])

    def test_one_failing_source_does_not_stop_others(self):
        def broken(*_):
            raise urllib.error.URLError("no network")
        with mock.patch.object(google, "fetch_calendar", broken), \
                mock.patch.object(google, "fetch_tasks", lambda: []), \
                mock.patch.object(google, "fetch_gmail", lambda: []), \
                mock.patch.object(google, "fetch_linkedin", lambda: []):
            with self.assertLogs("nudgy.google", "WARNING") as logged:
                items, errors = google.sync_all(NOW)
        self.assertIn("calendar failed: Offline", logged.output[0])
        self.assertEqual(errors, {"calendar": "Offline"})
        self.assertEqual(set(items), {"task", "gmail", "linkedin"})


class Auth(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": temp.name, "HOME": temp.name})
        env.start()
        self.addCleanup(env.stop)
        folder = Path(temp.name) / "nudgy"
        folder.mkdir()
        (folder / "google-client.json").write_text(json.dumps(
            {"installed": {"client_id": "id", "client_secret": "secret"}}))

    def browser(self, tamper_state=False):
        def open_url(url):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            self.seen = query
            state = "wrong" if tamper_state else query["state"][0]

            def visit():
                base = query["redirect_uri"][0]
                urllib.request.urlopen(f"{base}?state={state}&code=abc").read()
                if tamper_state:
                    urllib.request.urlopen(f"{base}?state={query['state'][0]}&error=access_denied").read()
            threading.Thread(target=visit).start()
        return mock.patch.object(google.webbrowser, "open", open_url)

    def test_sign_in_stores_private_token_and_asks_read_only(self):
        reply = {"access_token": "tok", "refresh_token": "ref", "expires_in": 3600}
        with self.browser(), mock.patch.object(google, "_post_token", return_value=reply) as post:
            google.authorize(timeout=10)
        self.assertEqual(post.call_args.args[0]["code"], "abc")
        self.assertIn("code_verifier", post.call_args.args[0])
        self.assertTrue(all(s.endswith(".readonly") for s in self.seen["scope"][0].split()))
        self.assertEqual(stat.S_IMODE(google.token_path().stat().st_mode), 0o600)
        self.assertEqual(google.access_token(), "tok")

    def test_callback_with_wrong_state_is_ignored(self):
        with self.browser(tamper_state=True), mock.patch.object(google, "_post_token") as post:
            with self.assertRaises(google.NeedsSignIn):
                google.authorize(timeout=10)
        post.assert_not_called()

    def test_refreshes_expired_token_and_keeps_refresh_token(self):
        google._write_token({"access_token": "old", "refresh_token": "ref", "expires_at": 0})
        with mock.patch.object(google, "_post_token", return_value={"access_token": "new", "expires_in": 3600}):
            self.assertEqual(google.access_token(), "new")
        self.assertEqual(json.loads(google.token_path().read_text())["refresh_token"], "ref")

    def test_revoked_token_asks_for_sign_in(self):
        google._write_token({"access_token": "old", "refresh_token": "ref", "expires_at": 0})
        error = urllib.error.HTTPError(google.TOKEN_URL, 400, "bad", {}, None)
        with mock.patch.object(google, "_post_token", side_effect=error):
            with self.assertRaises(google.NeedsSignIn):
                google.access_token()


if __name__ == "__main__":
    unittest.main()


class WriteTest(unittest.TestCase):
    """Adding to Google uses its own token and sends exactly what we expect."""

    def capture(self):
        sent = []

        def fake_send(method, url, body=None):
            sent.append((method, url, body))
            return {"id": "new-id"}
        return sent, mock.patch.object(google, "send", fake_send)

    def test_add_task_with_date(self):
        sent, patch = self.capture()
        with patch:
            made = google.add_task("Call the dentist", datetime(2026, 11, 20, 9, 0, tzinfo=timezone.utc))
        self.assertEqual(made, "new-id")
        self.assertEqual(sent, [("POST", google.TASKS, {"title": "Call the dentist", "due": "2026-11-20T00:00:00.000Z"})])

    def test_add_timed_and_all_day_events(self):
        sent, patch = self.capture()
        start = datetime(2026, 11, 14, 13, 0, tzinfo=timezone.utc)
        with patch:
            google.add_event("Lunch with Mia", start, 45)
            google.add_event("Conference", start, all_day=True)
        self.assertEqual(sent[0][2]["end"]["dateTime"], "2026-11-14T13:45:00+00:00")
        self.assertEqual(sent[1][2]["start"], {"date": "2026-11-14"})
        self.assertEqual(sent[1][2]["end"], {"date": "2026-11-15"})

    def test_undo_deletes(self):
        sent, patch = self.capture()
        with patch:
            google.remove("event", "abc")
        self.assertEqual(sent, [("DELETE", google.EVENTS + "/abc", None)])

    def test_write_token_is_separate(self):
        self.assertNotEqual(google.write_token_path(), google.token_path())
        self.assertNotIn("calendar.events", " ".join(google.SCOPES))
