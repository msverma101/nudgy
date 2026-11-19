"""Google Keep through the official Keep API (keep.googleapis.com), read-only.

Google describes this API as enterprise-only: it works for Google Workspace accounts whose admin
allows it, not for personal @gmail.com accounts. It uses its own sign-in and token file, so if
Google refuses Keep, Calendar, Tasks and Gmail keep working.

The API has no pinned or archived flags, so importance comes from the content: dates ahead,
urgent words, unticked checklist items and recent edits. Other notes are counted but not shown.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from . import google
from .claude_notes import _dates
from .model import Item, config_dir, data_dir, urgent_words

SCOPES = ["https://www.googleapis.com/auth/keep.readonly"]
NOTES_URL = "https://keep.googleapis.com/v1/notes"
RECENT_DAYS = 7
DATE_AHEAD_DAYS = 30
EXTRA_URGENT = re.compile(r"\b(important|asap|todo|to do|must|don't forget|remember)\b|!!", re.I)


class KeepRefused(Exception):
    """Google answered, but said no. The message is Google's reason in plain words."""


def token_path() -> Path:
    return config_dir() / "keep-token.json"


def summary_path() -> Path:
    return data_dir() / "keep-summary.json"


def connect() -> int:
    """Signs in with the Keep scope only, then proves access by listing notes."""
    google.authorize(scopes=SCOPES, path=token_path())
    return len(list_notes())


def disconnect() -> None:
    token_path().unlink(missing_ok=True)
    summary_path().unlink(missing_ok=True)


def _explain(error: urllib.error.HTTPError) -> str:
    try:
        body = json.loads(error.read().decode())["error"]
        message, status = body.get("message", ""), body.get("status", "")
        reasons = [d.get("reason", "") for d in body.get("details", []) if isinstance(d, dict)]
    except (ValueError, KeyError, AttributeError):
        message, status, reasons = "", "", []
    text = f"{error.code} {status}: {message}".strip()
    if "SERVICE_DISABLED" in reasons or "has not been used in project" in message:
        return f"The Keep API is not enabled in this Google Cloud project. {text}"
    if "ACCESS_TOKEN_SCOPE_INSUFFICIENT" in reasons:
        return f"The sign-in did not grant Keep access. {text}"
    return text or f"Google returned {error.code}"


def list_notes() -> list[dict]:
    notes, page = [], None
    while True:
        params = {"pageSize": 100, "filter": "trashed = false"}
        if page:
            params["pageToken"] = page
        request = urllib.request.Request(f"{NOTES_URL}?{urllib.parse.urlencode(params)}", headers={
            "Authorization": f"Bearer {google.access_token(token_path())}"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = json.load(response)
        except urllib.error.HTTPError as error:
            raise KeepRefused(_explain(error)) from None
        notes.extend(body.get("notes", []))
        page = body.get("nextPageToken")
        if not page:
            return notes


def _text(note: dict) -> tuple[str, list[str], int]:
    """Plain text, unticked checklist items, ticked count."""
    body = note.get("body", {})
    if "list" in body:
        open_items, done = [], 0
        stack = list(body["list"].get("listItems", []))
        while stack:
            entry = stack.pop(0)
            stack[:0] = entry.get("childListItems", [])
            if entry.get("checked"):
                done += 1
            else:
                open_items.append(entry.get("text", {}).get("text", "").strip())
        return "\n".join(open_items), [i for i in open_items if i], done
    return body.get("text", {}).get("text", ""), [], 0


def important(notes: list[dict], now: datetime) -> list[Item]:
    items = []
    for note in notes:
        if note.get("trashed"):
            continue
        text, open_items, done = _text(note)
        title = (note.get("title") or "").strip() or (text.strip().splitlines() or ["Untitled note"])[0]
        whole = f"{note.get('title', '')}\n{text}"
        upcoming = [d for d in _dates(whole, now.date())
                    if now.date() <= d <= now.date() + timedelta(days=DATE_AHEAD_DAYS)]
        urgent = bool(urgent_words().search(whole) or EXTRA_URGENT.search(whole))
        updated = note.get("updateTime")
        recent = bool(updated) and \
            datetime.fromisoformat(str(updated).replace("Z", "+00:00")) >= now - timedelta(days=RECENT_DAYS)
        if not (upcoming or urgent or open_items or recent):
            continue
        tags = [t for t, on in (("urgent", urgent), ("list", bool(open_items)), ("recent", recent)) if on]
        if open_items:
            detail = f"{len(open_items)} to do: " + ", ".join(open_items[:3]) + ("…" if len(open_items) > 3 else "")
        else:
            detail = " ".join(text.split())[:120]
        note_id = note.get("name", "").split("/")[-1]
        items.append(Item(id=f"keep:{note_id}", source="keep", title=title[:140], detail=detail,
                          due=min(upcoming).isoformat() if upcoming else None,
                          url=f"https://keep.google.com/#NOTE/{note_id}" if note_id else "https://keep.google.com/",
                          tags=tags))
    return items


def fetch_keep(now: datetime) -> list[Item] | None:
    if not token_path().exists():
        return None
    notes = list_notes()
    items = important(notes, now)
    summary_path().parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    summary_path().write_text(json.dumps({"shown": len(items), "total": len(notes)}))
    return items


def status() -> str:
    if not token_path().exists():
        if google.personal_account():
            return "Not available for personal Google accounts"
        return "Not connected"
    try:
        summary = json.loads(summary_path().read_text())
        return f"Connected · {summary['shown']} of {summary['total']} notes look important"
    except (FileNotFoundError, ValueError, KeyError):
        return "Connected"
