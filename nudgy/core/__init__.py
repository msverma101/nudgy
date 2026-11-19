"""The headless core: sync, change checks, the notes digest, undo history and status, without any GTK.

Clients (the GTK pet and list, and later the HTTP API) hold a `Core`, call its methods and listen to
its events. Where callbacks run is decided by the `Loop` the client passes in (see loop.py).
"""
from .engine import Core
from .loop import Loop, ThreadLoop

__all__ = ["Core", "Loop", "ThreadLoop"]
