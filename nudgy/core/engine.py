"""The core: one store, kept in sync with every source, with undo history and a status line.

Everything that changes the store runs on the loop's owning thread; fetching runs in the background
and hands its result back with call_soon. Clients listen with `listen(fn)`; fn gets one word:

- "items":   the list changed (sync, digest, undo, a change by hand)
- "status":  syncing / synced / a problem changed
- "history": undo or redo became possible or impossible
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from datetime import datetime

from .. import claude_notes, digest, sources
from ..changes import Changes
from ..model import Store, ticked_claude
from .loop import Loop

log = logging.getLogger(__name__)

CHECK_SECONDS = 60      # how often sources are asked whether anything new arrived
DIGEST_SECONDS = 3600   # how often new jotted notes are turned into steps
HISTORY = 100           # undo steps kept
HINT_SECONDS = 4        # how long a passing hint stays in the status line


def now() -> datetime:
    return datetime.now().astimezone()


class Core:
    def __init__(self, store: Store, loop: Loop, clock: Callable[[], datetime] = now,
                 labels: Mapping[str, str] | None = None, changes: Changes | None = None):
        self.store = store
        self.loop = loop
        self.clock = clock
        self.labels = dict(labels or {})        # source name -> name shown in problems
        self.changes = changes
        self.syncing = False
        self.pending: str | None = None         # "manual" / "auto": a sync asked for during another
        self.last_sync: datetime | None = None
        self.problem = ""
        self.digesting = False
        self.history: list[str] = []            # snapshots before each change, newest last
        self.redo_stack: list[str] = []
        self.current = store.snapshot()
        self._listeners: list[Callable[[str], object]] = []

    # ---- events ---------------------------------------------------------------------------------
    def listen(self, fn: Callable[[str], object]) -> None:
        self._listeners.append(fn)

    def emit(self, what: str) -> None:
        for fn in list(self._listeners):
            try:
                fn(what)
            except Exception:                   # a broken listener must not stop the others
                log.exception("listener failed on %s", what)

    def set_problem(self, text: str) -> None:
        self.problem = text
        self.emit("status")

    def hint(self, text: str, seconds: float = HINT_SECONDS) -> None:
        """A passing message in the status line that clears itself, unless something replaced it."""
        self.set_problem(text)

        def clear() -> None:
            if self.problem == text:
                self.set_problem("")
        self.loop.after(seconds, clear)

    # ---- running --------------------------------------------------------------------------------
    def start(self) -> None:
        """First sync now, then the regular change checks and digests."""
        if self.changes is None:
            self.changes = Changes()
        self.sync(manual=False)
        self.loop.every(CHECK_SECONDS, self.check)
        if digest.due():
            self.loop.after(90, self.digest)
        self.loop.every(DIGEST_SECONDS, self.digest)

    def sync(self, manual: bool = True) -> None:
        """Fetches everything now. A sync you start by hand also runs the notes digest."""
        if self.syncing:
            self.pending = self.pending or ("manual" if manual else "auto")    # run it right after
            return
        self.syncing = True
        self.emit("status")

        def work() -> None:
            moment = self.clock()
            items, errors = sources.sync_all(moment)
            self.loop.call_soon(self._synced, items, errors, moment, True, manual)
        self.loop.background(work)

    def check(self) -> None:
        """Asks each source whether anything is new, and fetches only those."""
        if self.syncing:
            return
        self.syncing = True
        changes = self.changes or Changes()
        self.changes = changes

        def work() -> None:
            groups = changes.due()
            if not groups:
                self.loop.call_soon(self._nothing_new)
                return
            moment = self.clock()
            items, errors = sources.sync_groups(moment, groups, calendar=self.store.synced.get("calendar"))
            self.loop.call_soon(self._synced, items, errors, moment, groups == set(sources.GROUPS), False)
        self.loop.background(work)

    def _nothing_new(self) -> None:
        self.syncing = False
        self.last_sync = self.clock()
        self.emit("status")
        self._run_pending()

    def _run_pending(self) -> None:
        pending, self.pending = self.pending, None
        if pending:
            self.sync(manual=pending == "manual")

    def _synced(self, items: dict, errors: dict, moment: datetime, full: bool, manual: bool) -> None:
        self.syncing = False
        if full and self.changes is not None:
            self.changes.full_done()
        if manual:
            self.digest()
        self.loop.call_soon(self._run_pending)
        self.store.synced.update(items)         # failed sources keep their last good items
        self.store.prune(moment)
        self.store.save()
        self.current = self.store.snapshot()    # tidying after a sync is not an undoable change
        if items:
            self.last_sync = moment
        reasons = set(errors.values())
        if len(reasons) == 1 and not items:
            self.problem = reasons.pop()
        else:
            self.problem = "; ".join(f"{self.labels.get(k, k)}: {v}" for k, v in errors.items())
        self.emit("items")
        self.emit("status")

    def digest(self) -> None:
        """Turns long jotted notes (like Keep event notes) into concrete steps, in the background."""
        if self.digesting:
            return
        self.digesting = True
        items = self.store.visible(self.clock())
        steps = self.store.steps

        def work() -> None:
            self.loop.call_soon(self._digested, digest.run(items, steps))
        self.loop.background(work)

    def _digested(self, result: dict) -> None:
        self.digesting = False
        if result.get("steps"):
            for item_id, steps in result["steps"].items():
                self.store.steps.setdefault(item_id, []).extend(steps)
            self.store.save()
            self.current = self.store.snapshot()    # suggestions are not an undoable change of yours
            self.emit("items")
        if result.get("error"):
            self.set_problem(f"Notes digest: {result['error']}")

    # ---- changes by hand, undo and redo ---------------------------------------------------------
    def changed(self) -> None:
        """Every change by hand ends here, so this is where undo history is recorded."""
        snapshot = self.store.snapshot()
        if snapshot != self.current:
            self._write_back(self.current, snapshot)
            self.history = (self.history + [self.current])[-HISTORY:]
            self.redo_stack.clear()
            self.current = snapshot
            self.emit("history")
        self.store.save()
        self.emit("items")

    def can_undo(self) -> bool:
        return bool(self.history)

    def undo(self) -> None:
        self._step(self.history, self.redo_stack, "Undo")

    def redo(self) -> None:
        self._step(self.redo_stack, self.history, "Redo")

    def _step(self, source: list[str], target: list[str], word: str) -> None:
        if not source:
            self.hint(f"Nothing to {word.lower()}", 3)
            return
        target.append(self.store.snapshot())
        previous, self.current = self.current, source.pop()
        self.store.restore(self.current)
        self._write_back(previous, self.current)
        self.store.save()
        self.emit("items")
        self.emit("history")

    def _write_back(self, before: str, after: str) -> None:
        """Mirror ticks of Claude follow-ups into the notes file, so they're closed there too."""
        old, new = ticked_claude(json.loads(before)), ticked_claude(json.loads(after))
        for item_id, done in [(i, True) for i in new - old] + [(i, False) for i in old - new]:
            try:
                if not claude_notes.set_ticked(item_id, done):
                    self.set_problem("That item changed in Open Items.md; ticked here only")
            except OSError as error:
                self.set_problem(f"Could not update Open Items.md: {error}")
