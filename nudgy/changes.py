"""Cheap checks for "has anything new arrived?", so a full fetch runs only when it's needed.

Every minute Nudgy asks each source a tiny question instead of fetching everything:
- Google: Gmail's history number, and whether any task list or calendar has an entry changed
  since the last check (one small request per list and calendar)
- Claude's notes files and the WhatsApp bridge's file: whether they changed on disk
- Signal: asking for new messages *is* the check, so it runs every 2 minutes
Sources that changed are fetched right away. Everything is also fetched in full once an hour, which
catches what the checks can't see (an event moving into the next 7 days, Luma, public events).
"""
from __future__ import annotations

import logging
import hashlib
import json
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from . import claude_notes, google
from .sources import GROUPS as FULL_GROUPS          # one list of sync groups, kept in sources.py
from .model import data_dir

log = logging.getLogger(__name__)

FULL_EVERY = 60 * 60
SIGNAL_EVERY = 2 * 60


def _stamp(moment: float) -> str:
    return datetime.fromtimestamp(moment, timezone.utc).isoformat().replace("+00:00", "Z")


def google_changed(since: float, last_history: str | None) -> tuple[bool, str | None]:
    """(changed?, Gmail history number). Raises if Google can't be reached."""
    history = google.api("https://gmail.googleapis.com/gmail/v1/users/me/profile").get("historyId")
    if last_history is not None and history != last_history:
        return True, history
    after = _stamp(since - 30)                 # a little overlap so nothing slips between checks
    base = "https://tasks.googleapis.com/tasks/v1"
    for task_list in google.api(f"{base}/users/@me/lists").get("items", []):
        found = google.api(f"{base}/lists/{task_list['id']}/tasks", {
            "updatedMin": after, "showCompleted": "true", "showHidden": "true", "showDeleted": "true",
            "maxResults": 1})
        if found.get("items"):
            return True, history
    base = "https://www.googleapis.com/calendar/v3"
    for calendar in google.api(f"{base}/users/me/calendarList").get("items", []):
        if not calendar.get("selected") or calendar["id"].endswith(google.SKIP_CALENDAR_KINDS):
            continue
        found = google.api(f"{base}/calendars/{urllib.parse.quote(calendar['id'], safe='')}/events", {
            "updatedMin": after, "showDeleted": "true", "maxResults": 1})
        if found.get("items"):
            return True, history
    return False, history


def _files_mark(paths: list[Path]) -> tuple:
    return tuple(p.stat().st_mtime_ns if p.exists() else 0 for p in paths)


def _whatsapp_mark() -> str:
    """The bridge rewrites its file often; only a change in the chats themselves counts."""
    try:
        db = json.loads((data_dir() / "whatsapp-chats.json").read_text())
    except (FileNotFoundError, ValueError):
        return ""
    db.pop("updated", None)
    return hashlib.sha1(json.dumps(db, sort_keys=True).encode()).hexdigest()


class Changes:
    def __init__(self):
        self.last_full = 0.0
        self.last_check = time.time()
        self.last_signal = 0.0
        self.history: str | None = None
        self.notes = _files_mark(claude_notes.paths())
        self.whatsapp = _whatsapp_mark()

    def full_done(self, moment: float | None = None) -> None:
        moment = moment or time.time()
        self.last_full = self.last_check = self.last_signal = moment
        self.notes = _files_mark(claude_notes.paths())
        self.whatsapp = _whatsapp_mark()

    def due(self) -> set[str]:
        """Groups (see sources.GROUPS) that have something new, or are due for their regular fetch."""
        moment = time.time()
        if moment - self.last_full >= FULL_EVERY:
            return set(FULL_GROUPS)
        groups = set()
        notes = _files_mark(claude_notes.paths())
        if notes != self.notes:
            self.notes = notes
            groups.add("claude")
        whatsapp = _whatsapp_mark()
        if whatsapp != self.whatsapp:
            self.whatsapp = whatsapp
            groups.add("whatsapp")
        if moment - self.last_signal >= SIGNAL_EVERY:
            self.last_signal = moment
            groups.add("signal")
        if google.token_path().exists():
            try:
                changed, self.history = google_changed(self.last_check, self.history)
            except google.NETWORK_ERRORS as error:
                log.info("Gmail change check skipped: %s", google.describe(error))
                changed = False            # offline or signed out: the hourly fetch reports it
            if changed:
                groups.add("google")
        self.last_check = moment
        return groups
