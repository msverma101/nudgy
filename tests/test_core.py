import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from nudgy import claude_notes, digest, sources
from nudgy.core import Core, Loop, ThreadLoop
from nudgy.model import Item, Store

T = datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc)


class StepLoop(Loop):
    """Runs everything at once on the calling thread; timers are only recorded."""

    def __init__(self):
        self.timers: list[tuple[str, float]] = []

    def call_soon(self, fn, *args):
        fn(*args)

    def after(self, seconds, fn):
        self.timers.append(("after", seconds))
        self.last_after = fn

    def every(self, seconds, fn):
        self.timers.append(("every", seconds))

    def background(self, fn):
        fn()


class Base(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name) / "state.json")
        self.loop = StepLoop()
        self.core = Core(self.store, self.loop, clock=lambda: T, labels={"gmail": "Gmail"})
        self.events: list[str] = []
        self.core.listen(self.events.append)


class Sync(Base):
    def test_full_sync_stores_items_and_reports(self):
        mail = Item("gmail:1", "gmail", "Reply to Ana", due=None)
        with mock.patch.object(sources, "sync_all", return_value=({"gmail": [mail]}, {})), \
                mock.patch.object(digest, "run", return_value={}) as run:
            self.core.sync(manual=False)
        self.assertEqual(self.store.synced["gmail"], [mail])
        self.assertFalse(self.core.syncing)
        self.assertEqual(self.core.last_sync, T)
        self.assertEqual(self.core.problem, "")
        self.assertIn("items", self.events)
        run.assert_not_called()                     # only a sync by hand runs the digest

    def test_manual_sync_runs_digest(self):
        with mock.patch.object(sources, "sync_all", return_value=({}, {})), \
                mock.patch.object(digest, "run", return_value={}) as run:
            self.core.sync(manual=True)
        run.assert_called_once()

    def test_failed_sources_keep_last_items_and_explain(self):
        old = Item("gmail:1", "gmail", "Old mail")
        self.store.synced["gmail"] = [old]
        with mock.patch.object(sources, "sync_all", return_value=({}, {"gmail": "Sign-in expired"})):
            self.core.sync(manual=False)
        self.assertEqual(self.store.synced["gmail"], [old])
        self.assertEqual(self.core.problem, "Sign-in expired")
        self.assertIsNone(self.core.last_sync)

    def test_several_problems_name_their_source(self):
        task = Item("task:1", "task", "Buy milk")
        errors = {"gmail": "offline", "luma": "bad link"}
        with mock.patch.object(sources, "sync_all", return_value=({"task": [task]}, errors)):
            self.core.sync(manual=False)
        self.assertEqual(self.core.problem, "Gmail: offline; luma: bad link")

    def test_sync_during_sync_runs_after(self):
        calls = []

        def slow(moment):
            calls.append(moment)
            if len(calls) == 1:
                self.core.sync(manual=True)         # asked for while the first one runs
            return {}, {}
        with mock.patch.object(sources, "sync_all", side_effect=slow), \
                mock.patch.object(digest, "run", return_value={}):
            self.core.sync(manual=False)
        self.assertEqual(len(calls), 2)
        self.assertIsNone(self.core.pending)

    def test_check_with_nothing_new(self):
        changes = mock.Mock()
        changes.due.return_value = set()
        self.core.changes = changes
        with mock.patch.object(sources, "sync_groups") as fetch:
            self.core.check()
        fetch.assert_not_called()
        self.assertFalse(self.core.syncing)
        self.assertEqual(self.core.last_sync, T)

    def test_check_fetches_only_changed_groups(self):
        changes = mock.Mock()
        changes.due.return_value = {"claude"}
        self.core.changes = changes
        with mock.patch.object(sources, "sync_groups", return_value=({}, {})) as fetch:
            self.core.check()
        self.assertEqual(fetch.call_args.args[1], {"claude"})
        changes.full_done.assert_not_called()

    def test_start_schedules_checks_and_digest(self):
        with mock.patch.object(sources, "sync_all", return_value=({}, {})), \
                mock.patch.object(digest, "due", return_value=True):
            self.core.changes = mock.Mock()
            self.core.start()
        self.assertEqual(self.loop.timers, [("every", 60), ("after", 90), ("every", 3600)])


class Digest(Base):
    def test_steps_added_without_undo_entry(self):
        with mock.patch.object(digest, "run", return_value={"steps": {"note:1": [{"text": "a", "done": False}]}}):
            self.core.digest()
        self.assertEqual(self.store.steps["note:1"], [{"text": "a", "done": False}])
        self.assertFalse(self.core.can_undo())
        self.assertFalse(self.core.digesting)

    def test_error_shown(self):
        with mock.patch.object(digest, "run", return_value={"error": "model not found"}):
            self.core.digest()
        self.assertEqual(self.core.problem, "Notes digest: model not found")


class History(Base):
    def test_undo_and_redo(self):
        self.store.add_note("Call the landlord", T)
        self.core.changed()
        self.assertTrue(self.core.can_undo())
        self.core.undo()
        self.assertEqual(self.store.notes, [])
        self.assertFalse(self.core.can_undo())
        self.core.redo()
        self.assertEqual([n.title for n in self.store.notes], ["Call the landlord"])
        self.assertIn("history", self.events)

    def test_new_change_clears_redo(self):
        self.store.add_note("One", T)
        self.core.changed()
        self.core.undo()
        self.store.add_note("Two", T)
        self.core.changed()
        self.assertEqual(self.core.redo_stack, [])

    def test_nothing_to_undo_is_a_passing_hint(self):
        self.core.undo()
        self.assertEqual(self.core.problem, "Nothing to undo")
        self.loop.last_after()
        self.assertEqual(self.core.problem, "")

    def test_hint_not_cleared_if_replaced(self):
        self.core.hint("first")
        self.core.set_problem("second")
        self.loop.last_after()
        self.assertEqual(self.core.problem, "second")

    def test_unchanged_store_adds_no_history(self):
        self.core.changed()
        self.assertFalse(self.core.can_undo())

    def test_ticks_of_claude_items_written_back(self):
        with mock.patch.object(claude_notes, "set_ticked", return_value=False) as tick:
            self.store.dismissed["claude:abc"] = T.isoformat()
            self.core.changed()
        tick.assert_called_once_with("claude:abc", True)
        self.assertEqual(self.core.problem, "That item changed in Open Items.md; ticked here only")

    def test_broken_listener_does_not_stop_others(self):
        self.core.listen(lambda _what: 1 / 0)
        seen = []
        self.core.listen(seen.append)
        with self.assertLogs("nudgy.core.engine", "ERROR"):
            self.core.set_problem("x")
        self.assertEqual(seen, ["status"])


class Threads(unittest.TestCase):
    def test_thread_loop_runs_calls_in_order_on_its_thread(self):
        loop = ThreadLoop().start()
        self.addCleanup(loop.stop)
        seen, done = [], threading.Event()
        loop.after(0.05, lambda: (seen.append("later"), done.set()))
        loop.call_soon(lambda: seen.append(threading.current_thread().name))
        self.assertTrue(done.wait(2))
        self.assertEqual(seen, ["nudgy-core", "later"])

    def test_repeating_task_survives_an_error(self):
        loop = ThreadLoop().start()
        self.addCleanup(loop.stop)
        runs, done = [], threading.Event()

        def flaky():
            runs.append(1)
            if len(runs) == 3:
                done.set()
            raise ValueError("boom")
        with self.assertLogs("nudgy.core.loop", "ERROR"):
            loop.every(0.01, flaky)
            self.assertTrue(done.wait(2))


if __name__ == "__main__":
    unittest.main()
