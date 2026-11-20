"""Where the Store keeps its data: one SQLite file, standard library only.

Each top-level section of the state (notes, overrides, synced items, ...) is one row. A save writes only
the sections that changed, in one transaction, so a crash never leaves half a save and a sync no longer
rewrites your notes. Sections you change by hand are also appended to a change log: the base for
multi-device edits once the core has its API."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sections (name TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS changes (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    device TEXT NOT NULL,
    section TEXT NOT NULL,
    body TEXT NOT NULL
);
"""
KEEP_CHANGES = 2000          # the log is for syncing devices and recent history, not an archive


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not path.exists():
            # Created private, because it holds email subjects. SQLite gives its -wal and -shm files
            # the same permissions.
            os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
        self.lock = threading.Lock()
        self.connection = sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)
        try:
            self.connection.execute("PRAGMA journal_mode=WAL")     # readers never wait for a writer
            # With WAL this never corrupts the file; a power cut can lose only the last save.
            self.connection.execute("PRAGMA synchronous=NORMAL")
            self.connection.executescript(SCHEMA)
        except sqlite3.DatabaseError:
            self.connection.close()
            raise
        self.written: dict[str, str] = {}

    def close(self) -> None:
        self.connection.close()

    def read(self) -> dict:
        with self.lock:
            rows = self.connection.execute("SELECT name, body FROM sections").fetchall()
        self.written = dict(rows)
        return {name: json.loads(body) for name, body in rows}

    def write(self, sections: dict, logged: frozenset[str] = frozenset(), device: str = "this computer") -> int:
        """Stores the sections that differ from what is on disk; returns how many."""
        bodies = {name: json.dumps(value, ensure_ascii=False) for name, value in sections.items()}
        changed = {name: body for name, body in bodies.items() if self.written.get(name) != body}
        if not changed:
            return 0
        at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self.connection.executemany(
                    "INSERT INTO sections (name, body) VALUES (?, ?) "
                    "ON CONFLICT(name) DO UPDATE SET body = excluded.body", changed.items())
                self.connection.executemany(
                    "INSERT INTO changes (at, device, section, body) VALUES (?, ?, ?, ?)",
                    [(at, device, name, body) for name, body in changed.items()
                     if name in logged and not (name not in self.written and body in ("{}", "[]"))])
                self.connection.execute(
                    "DELETE FROM changes WHERE seq <= (SELECT MAX(seq) FROM changes) - ?", (KEEP_CHANGES,))
                self.connection.execute("COMMIT")
            except BaseException:
                self.connection.execute("ROLLBACK")
                raise
        self.written.update(changed)
        return len(changed)

    def changes(self, since: int = 0) -> list[dict]:
        """Logged changes after sequence number `since`, oldest first."""
        with self.lock:
            rows = self.connection.execute(
                "SELECT seq, at, device, section, body FROM changes WHERE seq > ? ORDER BY seq", (since,)).fetchall()
        return [{"seq": seq, "at": at, "device": device, "section": section, "body": json.loads(body)}
                for seq, at, device, section, body in rows]
