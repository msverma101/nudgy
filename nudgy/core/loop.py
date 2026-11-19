"""Where the core's work runs.

The core never touches a toolkit. It asks its loop to run a function on the owning thread (so the
store is only ever changed from one thread), after a delay, every N seconds, or in the background.
The GTK app passes a loop built on GLib; ThreadLoop is the same thing without GTK.
"""
from __future__ import annotations

import heapq
import itertools
import logging
import threading
import time
from collections.abc import Callable

log = logging.getLogger(__name__)


class Loop:
    def call_soon(self, fn: Callable[..., object], *args) -> None:
        """Runs fn(*args) on the owning thread."""
        raise NotImplementedError

    def after(self, seconds: float, fn: Callable[[], object]) -> None:
        """Runs fn once on the owning thread after `seconds`."""
        raise NotImplementedError

    def every(self, seconds: float, fn: Callable[[], object]) -> None:
        """Runs fn on the owning thread every `seconds`, for as long as the loop runs."""
        raise NotImplementedError

    def background(self, fn: Callable[[], object]) -> None:
        """Runs fn on a worker thread. It must hand results back with call_soon."""
        threading.Thread(target=fn, daemon=True).start()


class ThreadLoop(Loop):
    """A loop without GTK: one owning thread that runs queued calls and timers in order."""

    def __init__(self) -> None:
        self._queue: list[tuple[float, int, Callable[[], object], float | None]] = []
        self._order = itertools.count()
        self._wake = threading.Condition()
        self._stopped = False
        self.thread: threading.Thread | None = None

    def _push(self, when: float, fn: Callable[[], object], repeat: float | None) -> None:
        with self._wake:
            heapq.heappush(self._queue, (when, next(self._order), fn, repeat))
            self._wake.notify()

    def call_soon(self, fn: Callable[..., object], *args) -> None:
        self._push(time.monotonic(), (lambda: fn(*args)), None)

    def after(self, seconds: float, fn: Callable[[], object]) -> None:
        self._push(time.monotonic() + seconds, fn, None)

    def every(self, seconds: float, fn: Callable[[], object]) -> None:
        self._push(time.monotonic() + seconds, fn, seconds)

    def start(self) -> ThreadLoop:
        self.thread = threading.Thread(target=self.run, daemon=True, name="nudgy-core")
        self.thread.start()
        return self

    def stop(self) -> None:
        with self._wake:
            self._stopped = True
            self._wake.notify()

    def run(self) -> None:
        while True:
            with self._wake:
                while not self._stopped and (not self._queue or self._queue[0][0] > time.monotonic()):
                    timeout = self._queue[0][0] - time.monotonic() if self._queue else None
                    self._wake.wait(timeout)
                if self._stopped:
                    return
                _when, _order, fn, repeat = heapq.heappop(self._queue)
            try:
                fn()
            except Exception:                   # one failing call must not stop the core
                log.exception("core task failed")
            if repeat is not None:
                self._push(time.monotonic() + repeat, fn, repeat)
