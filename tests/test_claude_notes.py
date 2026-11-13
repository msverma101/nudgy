import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from nudgy import claude_notes as cn
from nudgy.model import LATER, NOW, SOON, bucket, score

T = datetime(2026, 11, 11, 12, 0, tzinfo=timezone(timedelta(hours=1)))

OPEN = """# Open Items

## Short film (added 11 Nov 2026)
- [ ] URGENT (event starts Fri 13 Nov, 10:00): apply at https://example.org/apply.
- [ ] Check the rules. (11 Nov: the event page gives no length.)
- [x] Opened the editor (Works, 11 Nov).
- [ ] Retry linking WhatsApp; fixed 11 Nov.
- [ ] Apply for the robotics hackathon (21–22 Nov, Example Hall).
- [ ] Pottery course phase starts 25 Jan 2027.
- [ ] Watch the video.
  - [ ] nested detail is not an item

## Session handoffs
- [ ] **7 Nov 2026 · Example_project** · `cd /tmp && claude --resume 11111111-2222-4333-8444-555555555555`
  - **Status:** Findings pushed, PR not opened.
- [x] **6 Nov 2026 · Done thing** · `cd /tmp && claude --resume 66666666-7777-4888-9999-000000000000`
"""
DEADLINES = """# Application Deadlines

## Dated
- [ ] **Sat 7 Nov 2026:** Team pitch.
- [ ] **Sun 22 Nov 2026, 11:59 PM PT:** Sign-up closes.

## Missed
- [ ] Old cohort, deadline was 30 Oct 2026.
"""


class Reading(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.folder = Path(self.dir.name)
        (self.folder / "Open Items.md").write_text(OPEN)
        (self.folder / "Application Deadlines.md").write_text(DEADLINES)
        self.items = {i.title[:20]: i for i in cn.read(T, self.folder)}

    def tearDown(self):
        self.dir.cleanup()

    def test_only_unticked_top_level_lines_and_not_missed(self):
        self.assertEqual(len(self.items), 9)
        self.assertNotIn("Opened Blender (Work", self.items)
        self.assertNotIn("nested detail is not", self.items)
        self.assertNotIn("Old cohort, deadline", self.items)

    def test_dates(self):
        due = {k: i.due for k, i in self.items.items()}
        self.assertEqual(due["URGENT (event starts"], "2026-11-13")
        self.assertIsNone(due["Check the rules. (11"])            # a note's date stamp
        self.assertIsNone(due["Retry linking WhatsA"])            # "fixed 11 Nov" is the past
        self.assertEqual(due["Apply for the roboti"], "2026-11-21")
        self.assertEqual(due["Pottery course phase"], "2027-01-25")
        self.assertEqual(due["Sat 7 Nov 2026: Team"], "2026-11-07")   # deadline file: overdue stays
        self.assertEqual(due["Sun 22 Nov 2026, 11:"], "2026-11-22")

    def test_ranking(self):
        level = {k: bucket(score(i, T)) for k, i in self.items.items()}
        self.assertEqual(level["URGENT (event starts"], NOW)
        self.assertEqual(level["Sat 7 Nov 2026: Team"], NOW)
        self.assertEqual(level["Apply for the roboti"], SOON)
        self.assertEqual(level["Watch the video."], LATER)
        self.assertGreater(score(self.items["Watch the video."], T), score(self.items["Resume: Example_proj"], T))

    def test_links_and_sections(self):
        urgent = self.items["URGENT (event starts"]
        self.assertEqual(urgent.url, "https://example.org/apply")
        self.assertEqual(urgent.detail, "Short film")
        self.assertTrue(self.items["Watch the video."].url.startswith("file://"))
        handoff = self.items["Resume: Example_proj"]
        self.assertEqual(handoff.title, "Resume: Example_project (7 Nov)")
        self.assertEqual(handoff.detail, "Findings pushed, PR not opened.")
        self.assertEqual(handoff.url, "resume:/tmp|11111111-2222-4333-8444-555555555555")

    def test_tick_and_untick_write_the_file(self):
        item = self.items["Watch the video."]
        self.assertTrue(cn.set_ticked(item.id, True, self.folder))
        text = (self.folder / "Open Items.md").read_text()
        self.assertIn("- [x] Watch the video.", text)
        self.assertIn("  - [ ] nested detail is not an item", text)     # nothing else touched
        self.assertNotIn("Watch the video.", [i.title for i in cn.read(T, self.folder)])
        self.assertTrue(cn.set_ticked(item.id, False, self.folder))
        self.assertEqual((self.folder / "Open Items.md").read_text(), OPEN)
        self.assertFalse(cn.set_ticked("claude:nothing", True, self.folder))

    def test_obsidian_link_when_folder_is_a_vault(self):
        config = self.folder / "obsidian.json"
        config.write_text('{"vaults": {"a": {"path": "%s"}}}' % self.folder)
        with mock.patch.object(cn.Path, "home", return_value=self.folder / "home"):
            (self.folder / "home/.config/obsidian").mkdir(parents=True)
            (self.folder / "home/.config/obsidian/obsidian.json").write_text(config.read_text())
            items = cn.read(T, self.folder)
        self.assertTrue(next(i for i in items if i.title == "Watch the video.").url.startswith("obsidian://open?path="))

    def test_resume_rejects_bad_commands(self):
        with self.assertRaises(ValueError):
            cn.resume("resume:/tmp|not-a-session; rm -rf ~")
        with mock.patch.object(cn.subprocess, "Popen") as popen:
            cn.resume("resume:/tmp|11111111-2222-4333-8444-555555555555")
        argv = popen.call_args.args[0]
        start = argv.index("gnome-terminal")      # may be wrapped in its own systemd scope
        self.assertEqual(argv[start:start + 2], ["gnome-terminal", "--working-directory=/tmp"])
        self.assertIn("--resume 11111111-2222-4333-8444-555555555555", argv[-1])
