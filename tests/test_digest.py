import json
import os
import tempfile
import unittest
from unittest import mock

from nudgy import changes, digest
from nudgy.model import Item

JOTTED = Item(id="task:1", source="task", title="1. Graph databases talk  Good for linked data  Query languages "
              "compared Use cases:  Social networks Supply chains Fraud detection Limitations: "
              "Scaling writes - big joins", detail="My list")


class Isolated(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        patcher = mock.patch.dict(os.environ, {"XDG_DATA_HOME": self.folder.name,
                                               "XDG_CONFIG_HOME": self.folder.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.folder.cleanup)


class Digest(Isolated):
    def setUp(self):
        super().setUp()
        from nudgy import settings
        settings.update("digest", {"enabled": True})

    def test_off_until_turned_on(self):
        from nudgy import settings
        settings.path().unlink()
        self.assertFalse(digest.settings()["enabled"])
        self.assertEqual(digest.run([JOTTED], {}, ask=self.fail), {})

    def test_spots_jotted_notes_only(self):
        short = Item(id="task:2", source="task", title="Buy dark chocolate")
        mail = Item(id="gmail:1", source="gmail", title=JOTTED.title)
        self.assertEqual(digest.candidates([JOTTED, short, mail], {}, {}), [JOTTED])
        self.assertEqual(digest.candidates([JOTTED], {"task:1": [{"text": "mine"}]}, {}), [])

    def test_parse_keeps_known_ids_and_https_links(self):
        text = 'ok {"notes": [{"id": "task:1", "steps": [{"text": "Try geoopt Poincaré embeddings", ' \
               '"url": "https://github.com/geoopt/geoopt"}, {"text": "Read x", "url": "javascript:1"}]}, ' \
               '{"id": "task:9", "steps": [{"text": "nope"}]}]}'
        steps = digest.parse(text, {"task:1"})
        self.assertEqual(list(steps), ["task:1"])
        self.assertEqual(steps["task:1"][0]["text"], "✦ Try geoopt Poincaré embeddings")
        self.assertIn("url", steps["task:1"][0])
        self.assertNotIn("url", steps["task:1"][1])

    def test_runs_once_per_note_until_it_changes(self):
        calls = []

        def ask(notes, model):
            calls.append([n.id for n in notes])
            return json.dumps({"notes": [{"id": "task:1", "steps": [{"text": "Try it"}]}]}), 500

        first = digest.run([JOTTED], {}, ask)
        self.assertEqual(first["steps"]["task:1"][0]["text"], "✦ Try it")
        self.assertEqual(digest.run([JOTTED], {}, ask)["steps"], {})
        edited = Item(id="task:1", source="task", title=JOTTED.title + " more", detail="My list")
        digest.run([edited], {}, ask)
        self.assertEqual(len(calls), 2)
        self.assertFalse(digest.due())

    def test_failure_is_reported_and_retried(self):
        def broken(notes, model):
            raise RuntimeError("offline")

        self.assertIn("offline", digest.run([JOTTED], {}, broken)["error"])
        self.assertTrue(digest.due())


class Changes(Isolated):
    def test_whatsapp_timestamp_alone_is_not_a_change(self):
        path = changes.data_dir() / "whatsapp-chats.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"updated": 1, "chats": {"a": {"unread": 1}}}))
        before = changes._whatsapp_mark()
        path.write_text(json.dumps({"updated": 2, "chats": {"a": {"unread": 1}}}))
        self.assertEqual(changes._whatsapp_mark(), before)
        path.write_text(json.dumps({"updated": 3, "chats": {"a": {"unread": 2}}}))
        self.assertNotEqual(changes._whatsapp_mark(), before)

    def test_only_changed_sources_are_due(self):
        with mock.patch.object(changes.google, "token_path") as token, \
                mock.patch.object(changes, "google_changed", return_value=(True, "7")):
            token.return_value.exists.return_value = True
            watch = changes.Changes()
            self.assertEqual(watch.due(), set(changes.FULL_GROUPS))     # first time: everything
            watch.full_done()
            self.assertEqual(watch.due(), {"google"})
        with mock.patch.object(changes, "google_changed", return_value=(False, "7")):
            self.assertEqual(watch.due(), set())


if __name__ == "__main__":
    unittest.main()
