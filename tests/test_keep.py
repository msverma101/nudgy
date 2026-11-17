import io
import json
import os
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from unittest import mock

from nudgy import chats, claude_notes, google, keep, luma, sources
from nudgy.model import NOW, SOON, LATER, bucket, score

T = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
OLD = "2026-01-01T10:00:00Z"
NEW = "2026-11-10T10:00:00Z"


def note(nid, title="", text=None, items=None, updated=OLD, trashed=False):
    body = {"list": {"listItems": items}} if items is not None else {"text": {"text": text or ""}}
    return {"name": f"notes/{nid}", "title": title, "body": body, "updateTime": updated, "trashed": trashed}


NOTES = [
    note("a", "Shopping", items=[{"text": {"text": "milk"}, "checked": False},
                                 {"text": {"text": "eggs"}, "checked": True},
                                 {"text": {"text": "party"}, "checked": False,
                                  "childListItems": [{"text": {"text": "balloons"}, "checked": False}]}]),
    note("b", "Visa", "Appointment 18 Nov at the Ausländerbehörde"),
    note("c", "", "URGENT call the landlord"),
    note("d", "Recipe ideas", "pasta, curry", updated=NEW),
    note("e", "Old thoughts", "nothing pressing here"),
    note("f", "All done", items=[{"text": {"text": "x"}, "checked": True}]),
    note("g", "Gone", "deadline tomorrow", trashed=True),
    note("h", "Far off", "Wedding 20 Jun 2027"),
]


class Importance(unittest.TestCase):
    def setUp(self):
        self.items = {i.id: i for i in keep.important(NOTES, T)}

    def test_only_important_notes(self):
        self.assertEqual(sorted(self.items), ["keep:a", "keep:b", "keep:c", "keep:d"])

    def test_fields(self):
        shop = self.items["keep:a"]
        self.assertEqual(shop.detail, "3 to do: milk, party, balloons")
        self.assertEqual(shop.url, "https://keep.google.com/#NOTE/a")
        self.assertEqual(self.items["keep:b"].due, "2026-11-18")
        self.assertEqual(self.items["keep:c"].title, "URGENT call the landlord")

    def test_ranking(self):
        level = {k: bucket(score(i, T)) for k, i in self.items.items()}
        self.assertEqual(level, {"keep:a": SOON, "keep:b": SOON, "keep:c": NOW, "keep:d": LATER})


class Access(unittest.TestCase):
    def http_error(self, code, body):
        return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(json.dumps(body).encode()))

    def test_refusals_are_explained(self):
        disabled = self.http_error(403, {"error": {"message": "Google Keep API has not been used in project 1",
                                                   "status": "PERMISSION_DENIED"}})
        self.assertIn("not enabled", keep._explain(disabled))
        scope = self.http_error(403, {"error": {"message": "insufficient", "status": "PERMISSION_DENIED",
                                                "details": [{"reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}]}})
        self.assertIn("did not grant", keep._explain(scope))
        other = self.http_error(403, {"error": {"message": "The caller does not have permission",
                                                "status": "PERMISSION_DENIED"}})
        self.assertEqual(keep._explain(other), "403 PERMISSION_DENIED: The caller does not have permission")

    def test_list_pages_and_uses_its_own_token(self):
        pages = [{"notes": [NOTES[0]], "nextPageToken": "p2"}, {"notes": [NOTES[1]]}]
        seen = []

        def fake_open(request, timeout):
            seen.append(request.full_url)
            response = mock.MagicMock()
            response.__enter__.return_value = io.BytesIO(json.dumps(pages.pop(0)).encode())
            return response
        with mock.patch.object(google, "access_token", return_value="tok") as token, \
                mock.patch.object(keep.urllib.request, "urlopen", side_effect=fake_open):
            self.assertEqual(len(keep.list_notes()), 2)
        self.assertEqual(token.call_args.args[0], keep.token_path())
        self.assertIn("pageToken=p2", seen[1])
        self.assertIn("trashed+%3D+false", seen[0])

    def test_not_connected_is_skipped_and_refusal_reported(self):
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": home, "XDG_DATA_HOME": home}):
            self.assertIsNone(keep.fetch_keep(T))
            self.assertEqual(keep.status(), "Not connected")
            keep.token_path().parent.mkdir(parents=True, exist_ok=True)
            keep.token_path().write_text("{}")
            with mock.patch.object(google, "sync_all", return_value=({}, {})), \
                    mock.patch.object(chats, "fetch_signal", return_value=None), \
                    mock.patch.object(luma, "fetch_luma", return_value=None), \
                    mock.patch.object(claude_notes, "read", return_value=[]), \
                    mock.patch.object(keep, "list_notes", side_effect=keep.KeepRefused("403 nope")):
                items, errors = sources.sync_all(T)
            self.assertEqual(errors, {"keep": "403 nope"})
            with mock.patch.object(keep, "list_notes", return_value=NOTES):
                self.assertEqual(len(keep.fetch_keep(T)), 4)
            self.assertEqual(keep.status(), "Connected · 4 of 8 notes look important")
