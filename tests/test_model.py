import json
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nudgy.model import (Item, LATER, NOW, SOON, Store, bucket, friendly_time, important, parse_quick_add, ranked,
                           score)

TZ = timezone(timedelta(hours=1))
NOW_T = datetime(2026, 11, 6, 12, 0, tzinfo=TZ)


def at(**delta):
    return (NOW_T + timedelta(**delta)).isoformat()


class Scoring(unittest.TestCase):
    def test_current_event_beats_everything(self):
        event = Item("c", "calendar", "Standup", due=at(minutes=-5), end=at(minutes=25))
        overdue = Item("t", "task", "Tax", due="2026-11-01")
        self.assertGreater(score(event, NOW_T), score(overdue, NOW_T))

    def test_time_buckets(self):
        self.assertEqual(bucket(score(Item("a", "task", "x", due="2026-11-05"), NOW_T)), NOW)
        self.assertEqual(bucket(score(Item("a", "calendar", "x", due=at(hours=1)), NOW_T)), NOW)
        self.assertEqual(bucket(score(Item("a", "task", "x", due="2026-11-06"), NOW_T)), SOON)
        self.assertEqual(bucket(score(Item("a", "task", "x", due="2026-11-07"), NOW_T)), SOON)
        self.assertEqual(bucket(score(Item("a", "task", "x", due="2026-11-10"), NOW_T)), LATER)
        self.assertEqual(bucket(score(Item("a", "note", "x"), NOW_T)), LATER)

    def test_all_day_item_due_today_is_not_overdue(self):
        item = Item("a", "task", "x", due="2026-11-06")
        self.assertEqual(friendly_time(item, NOW_T), "Today")

    def test_all_day_event_today_is_not_happening_now(self):
        birthday = Item("a", "calendar", "Birthday", due="2026-11-06", end="2026-11-07")
        self.assertEqual(bucket(score(birthday, NOW_T)), SOON)
        self.assertEqual(friendly_time(birthday, NOW_T), "Today")

    def test_event_time_shown_in_local_zone(self):
        item = Item("a", "calendar", "Call", due="2026-11-06T22:00:00-05:00")
        self.assertEqual(friendly_time(item, NOW_T), "Tomorrow 04:00")

    def test_mail_ordering(self):
        starred = Item("a", "gmail", "Hi", tags=["starred"])
        important = Item("b", "gmail", "Hi", tags=["important"])
        urgent = Item("c", "gmail", "Deadline for visa form", tags=["important"])
        self.assertGreater(score(starred, NOW_T), score(important, NOW_T))
        self.assertEqual(bucket(score(urgent, NOW_T)), NOW)

    def test_word_match_is_whole_word(self):
        self.assertEqual(score(Item("a", "gmail", "Our special offer", tags=["important"]), NOW_T), 60)

    def test_linkedin(self):
        message = Item("a", "linkedin", "New message", tags=["message"])
        job = Item("b", "linkedin", "10 new jobs", tags=["job"])
        interview = Item("c", "linkedin", "Interview next week?", tags=["message"])
        self.assertGreater(score(message, NOW_T), score(job, NOW_T))
        self.assertEqual(bucket(score(interview, NOW_T)), NOW)

    def test_override_wins(self):
        item = Item("a", "task", "x", due="2026-11-05")
        self.assertEqual(bucket(score(item, NOW_T, "low")), LATER)
        self.assertEqual(bucket(score(Item("b", "note", "y"), NOW_T, "high")), NOW)

    def test_ranked_mixes_naive_and_aware_dates(self):
        items = [Item("a", "task", "x", due="2026-11-06"), Item("b", "calendar", "y", due=at(hours=1))]
        self.assertEqual([i.id for i, _ in ranked(items, NOW_T, {})], ["b", "a"])


class QuickAdd(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(parse_quick_add("buy milk", NOW_T), ("buy milk", None, None))

    def test_bang_and_date(self):
        self.assertEqual(parse_quick_add("!pay rent tomorrow", NOW_T), ("pay rent", "2026-11-07", "high"))
        self.assertEqual(parse_quick_add("pay rent today!", NOW_T), ("pay rent", "2026-11-06", "high"))

    def test_word_alone_stays_the_title(self):
        self.assertEqual(parse_quick_add("today", NOW_T), ("today", None, None))


class Storage(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "state.json"

    def test_round_trip_and_private_file(self):
        store = Store(self.path)
        store.add_note("!call bank today", NOW_T)
        store.synced["gmail"] = [Item("gmail:1", "gmail", "Hello", tags=["important"])]
        store.save()
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        again = Store(self.path)
        self.assertEqual(again.notes[0].title, "call bank")
        self.assertEqual(again.overrides[again.notes[0].id], "high")
        self.assertEqual(again.synced["gmail"][0].tags, ["important"])

    def test_finish_and_undo(self):
        store = Store(self.path)
        note = store.add_note("x", NOW_T)
        mail = Item("gmail:1", "gmail", "Hello")
        store.synced["gmail"] = [mail]
        store.finish(note, NOW_T)
        store.finish(mail, NOW_T)
        self.assertEqual(store.visible(NOW_T), [])
        store.undo_finish(note)
        store.undo_finish(mail)
        self.assertEqual(len(store.visible(NOW_T)), 2)

    def test_dismissed_item_stays_hidden_after_resync(self):
        store = Store(self.path)
        mail = Item("gmail:1", "gmail", "Hello")
        store.finish(mail, NOW_T)
        store.synced["gmail"] = [Item("gmail:1", "gmail", "Hello")]
        self.assertEqual(store.visible(NOW_T), [])

    def test_finished_event_disappears_without_a_sync(self):
        store = Store(self.path)
        store.synced["calendar"] = [Item("c:1", "calendar", "Old", due=at(hours=-2), end=at(hours=-1)),
                                    Item("c:2", "calendar", "Now", due=at(hours=-1), end=at(hours=1))]
        self.assertEqual([i.id for i in store.visible(NOW_T)], ["c:2"])

    def test_corrupt_state_is_preserved(self):
        self.path.write_text("{not json")
        store = Store(self.path)
        self.assertEqual(store.notes, [])
        self.assertTrue(self.path.with_suffix(".corrupt").exists())

    def test_prune(self):
        store = Store(self.path)
        store.dismissed = {"old": (NOW_T - timedelta(days=90)).isoformat(), "new": NOW_T.isoformat()}
        store.overrides = {"gone": "high"}
        store.prune(NOW_T)
        self.assertEqual(list(store.dismissed), ["new"])
        self.assertEqual(store.overrides, {})


if __name__ == "__main__":
    unittest.main()


class Steps(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "state.json"

    def tearDown(self):
        self.dir.cleanup()

    def test_add_toggle_and_progress(self):
        store = Store(self.path)
        note = store.add_note("Apply to hackathon", NOW_T)
        self.assertTrue(store.add_step(note.id, " write bio "))
        self.assertFalse(store.add_step(note.id, "   "))
        store.add_step(note.id, "record clip")
        store.toggle_step(note.id, 0)
        self.assertEqual(store.steps[note.id][0], {"text": "write bio", "done": True})
        self.assertEqual(store.step_progress(note.id), "1/2 steps")
        self.assertEqual(store.step_progress("nothing"), "")

    def test_steps_survive_save_and_sync(self):
        store = Store(self.path)
        store.synced = {"gmail": [Item("gmail:1", "gmail", "Offer letter")]}
        store.add_step("gmail:1", "read contract")
        store.save()
        again = Store(self.path)
        again.synced = {"gmail": [Item("gmail:1", "gmail", "Offer letter")]}   # a fresh sync
        again.prune(NOW_T)
        self.assertEqual(again.steps["gmail:1"], [{"text": "read contract", "done": False}])

    def test_steps_dropped_when_item_gone(self):
        store = Store(self.path)
        store.add_step("gmail:old", "reply")
        store.prune(NOW_T)
        self.assertEqual(store.steps, {})

    def test_edit_remove_and_clear(self):
        store = Store(self.path)
        for text in ("a", "b", "c"):
            store.add_step("n", text)
        store.edit_step("n", 1, "B")
        store.edit_step("n", 1, "  ")          # blank edit keeps the old text
        store.toggle_step("n", 0)
        store.clear_done_steps("n")
        self.assertEqual([s["text"] for s in store.steps["n"]], ["B", "c"])
        store.remove_step("n", 0)
        store.remove_step("n", 0)
        self.assertNotIn("n", store.steps)

    def test_old_state_file_without_steps_loads(self):
        self.path.write_text(json.dumps({"notes": [], "synced": {}}))
        self.assertEqual(Store(self.path).steps, {})


class Snapshots(unittest.TestCase):
    def test_restore_brings_back_notes_priorities_steps_and_ticks(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "state.json")
            note = store.add_note("!call landlord", NOW_T)
            store.add_step(note.id, "find number")
            store.synced = {"gmail": [Item("gmail:1", "gmail", "Offer")]}
            before = store.snapshot()
            store.finish(note, NOW_T)
            store.finish(store.synced["gmail"][0], NOW_T)
            store.remove_step(note.id, 0)
            self.assertNotEqual(store.snapshot(), before)
            store.restore(before)
            self.assertEqual([n.title for n in store.notes], ["call landlord"])
            self.assertEqual(store.overrides, {note.id: "high"})
            self.assertEqual(store.steps[note.id][0]["text"], "find number")
            self.assertEqual(store.dismissed, {})
            self.assertEqual(len(store.synced["gmail"]), 1)     # synced items are not part of undo


class Placing(unittest.TestCase):
    def test_place_between(self):
        from nudgy.model import place_between
        self.assertEqual(place_between(None, None), 0.0)
        self.assertEqual(place_between(None, -60.0), -61.0)
        self.assertEqual(place_between(-40.0, None), -39.0)
        self.assertEqual(place_between(-60.0, -40.0), -50.0)

    def test_placed_items_keep_their_spot_others_follow_urgency(self):
        a = Item("a", "task", "A", due="2026-11-06")        # due today: 70
        b = Item("b", "note", "B")                           # 40
        c = Item("c", "task", "C", due="2026-11-05")         # overdue: 90
        self.assertEqual([i.id for i, _ in ranked([a, b, c], NOW_T, {})], ["c", "a", "b"])
        order = [i.id for i, _ in ranked([a, b, c], NOW_T, {}, {"b": -95.0})]   # dragged to the top
        self.assertEqual(order, ["b", "c", "a"])

    def test_medium_keeps_item_in_soon(self):
        overdue = Item("a", "task", "x", due="2026-11-01")
        note = Item("b", "note", "y")
        self.assertEqual(bucket(score(overdue, NOW_T, "medium")), SOON)
        self.assertEqual(bucket(score(note, NOW_T, "medium")), SOON)

    def test_places_are_saved_pruned_and_undoable(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "state.json")
            note = store.add_note("x", NOW_T)
            before = store.snapshot()
            store.places = {note.id: -3.0, "gone": 1.0}
            store.prune(NOW_T)
            self.assertEqual(store.places, {note.id: -3.0})
            store.save()
            self.assertEqual(Store(Path(folder) / "state.json").places, {note.id: -3.0})
            store.restore(before)
            self.assertEqual(store.places, {})


class ImportantOnly(unittest.TestCase):
    def keeps(self, item, override=None):
        return important(item, score(item, NOW_T, override))

    def test_keeps_what_needs_attention_today(self):
        self.assertTrue(self.keeps(Item("e", "luma", "Meetup", due=at(hours=1), end=at(hours=3))))
        self.assertTrue(self.keeps(Item("e2", "calendar", "Dinner", due=at(hours=6))))
        self.assertTrue(self.keeps(Item("t", "task", "Tax", due="2026-11-01")))
        self.assertTrue(self.keeps(Item("m", "gmail", "Interview on Monday", tags=["important"])))
        self.assertTrue(self.keeps(Item("s", "gmail", "Flat contract", tags=["starred"])))
        self.assertTrue(self.keeps(Item("w", "whatsapp", "Urgent: call me")))
        self.assertTrue(self.keeps(Item("n", "note", "buy milk")))
        self.assertTrue(self.keeps(Item("x", "gmail", "Newsletter"), override="high"))

    def test_hides_the_rest(self):
        self.assertFalse(self.keeps(Item("e", "luma", "Hackathon", due=at(days=10))))
        self.assertFalse(self.keeps(Item("m", "gmail", "Your invoice", tags=["important"])))
        self.assertFalse(self.keeps(Item("w", "whatsapp", "So you are in Germany now?")))
        self.assertFalse(self.keeps(Item("l", "linkedin", "New message", tags=["message"])))
        self.assertFalse(self.keeps(Item("c", "claude", "Decide later", tags=["handoff"])))
        self.assertFalse(self.keeps(Item("t", "task", "Brain dump", due="2026-11-07")))
        self.assertFalse(self.keeps(Item("n", "note", "someday"), override="low"))


class QuickAddTimesTest(unittest.TestCase):
    now = datetime(2026, 11, 13, 11, 0, tzinfo=timezone(timedelta(hours=1)))       # a Friday

    def check(self, text, title, due):
        from nudgy.model import parse_quick_add
        self.assertEqual(parse_quick_add(text, self.now)[:2], (title, due), text)

    def test_spoken_times(self):
        self.check("Call the bank tomorrow at 10", "Call the bank", "2026-11-14T10:00:00+01:00")
        self.check("call the bank tomorrow 10:00", "call the bank", "2026-11-14T10:00:00+01:00")
        self.check("Zahnarzt morgen um 9", "Zahnarzt", "2026-11-14T09:00:00+01:00")
        self.check("Abgabe Freitag 14:30 Uhr", "Abgabe", "2026-11-20T14:30:00+01:00")
        self.check("meeting at 3pm", "meeting", "2026-11-13T15:00:00+01:00")
        self.check("yoga at 9", "yoga", "2026-11-14T09:00:00+01:00")          # 9:00 has passed today
        self.check("dinner with Mia tonight", "dinner with Mia", "2026-11-13T20:00:00+01:00")

    def test_days(self):
        self.check("buy train tickets on friday", "buy train tickets", "2026-11-20")
        self.check("send report in 3 days", "send report", "2026-11-16")
        self.check("day after tomorrow submit form", "submit form", "2026-11-15")
        self.check("pay rent today", "pay rent", "2026-11-13")

    def test_numbers_that_are_not_times(self):
        self.check("buy 2 tickets", "buy 2 tickets", None)
        self.check("Room 2.14 booking", "Room 2.14 booking", None)


class QuickAddDatesTest(QuickAddTimesTest):
    def test_explicit_dates(self):
        self.check("dentist on the 20th of November", "dentist", "2026-11-20")
        self.check("conference Nov 20 2026", "conference", "2026-11-20")
        self.check("Zahnarzt am 20.11. um 9", "Zahnarzt", "2026-11-20T09:00:00+01:00")
        self.check("party on December 5th at 8pm", "party", "2026-12-05T20:00:00+01:00")
        self.check("trip 3 March", "trip", "2027-03-03")                     # past this year: next year

    def test_bare_hour_after_a_day(self):
        self.check("call with Alex tomorrow 11", "call with Alex", "2026-11-14T11:00:00+01:00")
        self.check("lunch monday 1", "lunch", "2026-11-16T13:00:00+01:00")
        self.check("buy 3 tickets tomorrow", "buy 3 tickets", "2026-11-14")


class StateVersionTest(unittest.TestCase):
    def test_unknown_fields_and_sections_survive(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            path.write_text(json.dumps({
                "version": 99, "future_section": {"keep": True},
                "notes": [{"id": "note:1", "source": "note", "title": "Pay rent", "colour": "blue"}],
                "synced": {"task": [{"id": "task:1", "source": "task", "title": "Call", "new_field": 1}]}}))
            store = Store(path)
            self.assertEqual([n.title for n in store.notes], ["Pay rent"])
            self.assertEqual(store.synced["task"][0].title, "Call")
            store.save()
            saved = json.loads(path.read_text())
            self.assertEqual(saved["future_section"], {"keep": True})
            self.assertEqual(saved["version"], 99)            # not downgraded

    def test_old_file_gets_a_version(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            path.write_text(json.dumps({"notes": []}))
            store = Store(path)
            store.save()
            self.assertEqual(json.loads(path.read_text())["version"], 1)
