import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nudgy.model import Item, Store, ticked_claude

NOW_T = datetime(2026, 11, 12, 12, 0, tzinfo=timezone(timedelta(hours=1)))


class Subtasks(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.folder.name) / "state.json")
        self.parent = self.store.add_note("Apply to Filmhack", NOW_T)
        self.child = self.store.add_note("Export the Garden clip", NOW_T)

    def tearDown(self):
        self.folder.cleanup()

    def test_note_becomes_step_and_comes_back(self):
        self.store.add_step(self.child.id, "render at 1080p")
        self.assertTrue(self.store.nest(self.child, self.parent.id, NOW_T))
        self.assertEqual([n.id for n in self.store.notes], [self.parent.id])
        steps = self.store.steps[self.parent.id]
        self.assertEqual([s["text"] for s in steps], ["Export the Garden clip", "render at 1080p"])
        back = self.store.unnest(self.parent.id, 0, NOW_T)
        self.assertEqual(back.id, self.child.id)
        self.assertIn(self.child.id, [n.id for n in self.store.notes])

    def test_synced_item_keeps_link_and_reappears(self):
        mail = Item(id="gmail:1", source="gmail", title="Reply to Alex", url="https://mail.google.com/x")
        self.store.synced["gmail"] = [mail]
        self.store.nest(mail, self.parent.id, NOW_T)
        self.assertNotIn(mail.id, [i.id for i in self.store.visible(NOW_T)])
        self.assertEqual(self.store.steps[self.parent.id][0]["url"], "https://mail.google.com/x")
        self.store.prune(NOW_T + timedelta(days=90))      # stays hidden while it is a step
        self.assertIn(mail.id, self.store.dismissed)
        self.store.unnest(self.parent.id, 0, NOW_T)
        self.assertIn(mail.id, [i.id for i in self.store.visible(NOW_T)])

    def test_cannot_nest_into_itself(self):
        self.assertFalse(self.store.nest(self.parent, self.parent.id, NOW_T))

    def test_undo_round_trip(self):
        before = self.store.snapshot()
        self.store.nest(self.child, self.parent.id, NOW_T)
        self.store.restore(before)
        self.assertEqual(len(self.store.notes), 2)
        self.assertNotIn(self.parent.id, self.store.steps)

    def test_claude_item_moved_is_not_ticked_until_its_step_is(self):
        claude = Item(id="claude:abc", source="claude", title="Add LICENSE")
        self.store.synced["claude"] = [claude]
        self.store.nest(claude, self.parent.id, NOW_T)
        snap = json.loads(self.store.snapshot())
        self.assertEqual(ticked_claude(snap), set())
        self.store.toggle_step(self.parent.id, 0)
        self.assertEqual(ticked_claude(json.loads(self.store.snapshot())), {"claude:abc"})


class DropZones(unittest.TestCase):
    def test_thirds(self):
        from nudgy.app import Window
        self.assertEqual(Window.drop_zone(100, 60, 105), "before")
        self.assertEqual(Window.drop_zone(100, 60, 130), "inside")
        self.assertEqual(Window.drop_zone(100, 60, 155), "after")


if __name__ == "__main__":
    unittest.main()


class Searching(unittest.TestCase):
    def test_matches_every_word_in_title_detail_source_and_steps(self):
        from nudgy.model import search
        a = Item(id="task:1", source="task", title="Graph databases talk notes", detail="My list")
        b = Item(id="gmail:2", source="gmail", title="Reply to Alex", detail="Physics team summary")
        c = Item(id="note:3", source="note", title="Filmhack")
        entries = [(a, 40), (b, 30), (c, 20)]
        steps = {"note:3": [{"text": "Upload Garden clip to Drive", "done": False}]}
        labels = {"gmail": "Gmail", "task": "Google Tasks", "note": "Note"}
        self.assertEqual([i.id for i, _ in search(entries, "GRAPH", steps, labels)], ["task:1"])
        self.assertEqual([i.id for i, _ in search(entries, "alex physics", steps, labels)], ["gmail:2"])
        self.assertEqual([i.id for i, _ in search(entries, "garden", steps, labels)], ["note:3"])
        self.assertEqual([i.id for i, _ in search(entries, "gmail", steps, labels)], ["gmail:2"])
        self.assertEqual(search(entries, "  ", steps, labels), [])
        self.assertEqual(search(entries, "alex kettle", steps, labels), [])


class LinkSafetyTest(unittest.TestCase):
    def test_only_own_notes_may_open_local_links(self):
        from nudgy.app import _trusted_link
        cases = [("potential", "obsidian://advanced-uri?commandid=x", False), ("luma", "file:///etc/passwd", False),
                 ("gmail", "resume:/tmp|" + "a" * 36, False), ("potential", "http://example.org", False),
                 ("claude", "resume:/home|x", True), ("note", "file:///home/x.md", True),
                 ("gmail", "https://mail.google.com/x", True), ("task", "", True)]
        for source, url, expected in cases:
            self.assertEqual(_trusted_link(Item(id="x", source=source, title="t", url=url)), expected, (source, url))
