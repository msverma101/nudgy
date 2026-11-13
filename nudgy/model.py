"""Items, priority ranking and local storage. No UI or network code here."""
from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta
from pathlib import Path

NOW, SOON, LATER = "Now", "Soon", "Later"
LEVELS = ("high", "medium", "normal", "low")     # normal = automatic; medium keeps an item in Soon
CHAT_SOURCES = ("signal", "whatsapp")
EVENT_SOURCES = ("calendar", "luma")
URGENT_WORDS = re.compile(
    r"\b(urgent|asap|deadline|action required|interview|job offer|offer letter|final notice|overdue)\b", re.I)


def quiet_claude_env() -> dict:
    """Environment for running the `claude` command: no usage statistics, error reports or update checks,
    so only the request itself goes to Anthropic."""
    return {**os.environ, "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1"}


def data_dir() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "nudgy"


def config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "nudgy"


@dataclass
class Item:
    id: str
    # note | claude | task | calendar | luma | gmail | linkedin | signal | whatsapp | potential
    source: str
    title: str
    detail: str = ""
    due: str | None = None         # ISO datetime, or ISO date when all-day
    end: str | None = None         # calendar events only
    url: str = ""
    tags: list[str] = field(default_factory=list)   # starred, important, message, invitation, job

    @classmethod
    def from_dict(cls, data: dict) -> Item:
        """Builds an item from saved data, ignoring fields this version doesn't know (a newer Nudgy may
        have written them), so an older version never crashes on a newer file."""
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    @property
    def all_day(self) -> bool:
        return bool(self.due) and "T" not in self.due


def _when(value: str, now: datetime) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=now.tzinfo) if parsed.tzinfo is None else parsed.astimezone(now.tzinfo)


def _time_score(item: Item, now: datetime) -> int | None:
    if not item.due:
        return None
    due = _when(item.due, now)
    if item.source in EVENT_SOURCES and not item.all_day and due <= now:
        return 95                                   # happening right now
    if item.all_day:
        days = (due.date() - now.date()).days
        return 90 if days < 0 else 70 if days == 0 else 50 if days == 1 else 30
    left = due - now
    if left < timedelta(0):
        return 90
    if left <= timedelta(hours=2):
        return 85
    if due.date() == now.date():
        return 70
    return 50 if (due.date() - now.date()).days == 1 else 30


def _message_score(item: Item) -> int:
    if item.source == "linkedin":
        score = {"message": 55, "invitation": 25, "job": 20}.get(next(iter(item.tags), ""), 20)
    elif item.source in CHAT_SOURCES:
        score = 40 if "group" in item.tags else 60      # people writing to you directly come first
    else:
        score = 75 if "starred" in item.tags else 60 if "important" in item.tags else 40
    return min(score + 25, 88) if URGENT_WORDS.search(item.title) else score


def score(item: Item, now: datetime, override: str | None = None) -> int:
    """Higher is more pressing. A manual override always wins over the automatic score."""
    timed = _time_score(item, now)
    if item.source in ("gmail", "linkedin", *CHAT_SOURCES):
        value = _message_score(item)
    elif item.source == "keep":
        if item.due:
            days = (_when(item.due, now).date() - now.date()).days
            value = 85 if days == 0 else 80 if days <= 2 else 65 - days if days <= 14 else 46
        else:
            value = 55 if "list" in item.tags else 45 if "recent" in item.tags else 42
        if "urgent" in item.tags:
            value = max(value, 80)
    elif item.source == "claude":
        # Open follow-ups rank a little above plain notes; resuming a session ranks below them.
        # A dated follow-up needs doing before the date, so anything due within two weeks is Soon.
        if item.due:
            days = (_when(item.due, now).date() - now.date()).days
            value = 90 if days < 0 else 85 if days == 0 else 80 if days <= 2 else 65 - days if days <= 14 else 46
        else:
            value = 35 if "handoff" in item.tags else 45
        if "urgent" in item.tags:
            value = max(value, 85)
    elif item.source == "potential":
        # Ideas, not commitments: always Later, ordered by how well they fit you.
        fit = next((int(t[4:]) for t in item.tags if t.startswith("fit:")), 0)
        value = 20 + fit * 29 // 100
    else:
        value = 40 if timed is None else timed
    if override == "high":
        return max(value, 80) + 10
    if override == "medium":
        return min(max(value, 50), 79)
    if override == "low":
        return min(value, 15)
    return value


def bucket(value: int) -> str:
    return NOW if value >= 80 else SOON if value >= 50 else LATER


IMPORTANT = 70     # Now, plus anything due today and starred mail


def important(item: Item, value: int) -> bool:
    """What the 'Important only' view keeps: things needing attention today, and your own notes
    unless you set them to low. Plain inbox mail, chats without urgent words, undated follow-ups
    and events from tomorrow on stay hidden until they get close."""
    if item.source == "note":
        return value > 15
    if item.source == "potential":
        return "top" in item.tags or value >= IMPORTANT       # the best few event ideas, or ones you set high
    return value >= IMPORTANT


def place_key(item: Item, value: int, places: dict[str, float]) -> float:
    """Position in a list: where you dragged it, otherwise by urgency (more urgent = smaller)."""
    return places.get(item.id, -float(value))


def ranked(items: list[Item], now: datetime, overrides: dict[str, str],
           places: dict[str, float] | None = None) -> list[tuple[Item, int]]:
    places = places or {}
    scored = [(item, score(item, now, overrides.get(item.id))) for item in items]
    far_future = "9999"
    return sorted(scored, key=lambda pair: (place_key(pair[0], pair[1], places),
                                            pair[0].due or far_future, pair[0].title.lower()))


def place_between(before: float | None, after: float | None) -> float:
    """A position between two neighbours; None means the start or end of the list."""
    if before is None and after is None:
        return 0.0
    if before is None:
        return after - 1
    if after is None:
        return before + 1
    return (before + after) / 2


WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
            "montag": 0, "dienstag": 1, "mittwoch": 2, "donnerstag": 3, "freitag": 4, "samstag": 5, "sonntag": 6,
            "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_DAY = (r"(?P<day>the day after tomorrow|day after tomorrow|übermorgen|today|tonight|tomorrow|heute|morgen|"
        r"in (?P<n>\d+|two|three|four|five|six|seven) days|in (?P<nd>\d+) tagen|"
        r"(?:next |this |nächsten |kommenden )?(?P<wd>" + "|".join(sorted(WEEKDAYS, key=len, reverse=True)) + r"))")
_TIME = (r"(?P<h>\d{1,2})(?:[:.](?P<m>\d{2}))?\s*(?P<ap>am|pm|a\.m\.|p\.m\.)?(?:\s*uhr)?")
_SMALL = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7}


def find_relative(text: str):
    """'in an hour', 'in 30 minutes', 'in 2 hours', 'in einer Stunde' -> (minutes, match)."""
    match = re.search(r"\bin\s+(?P<n>an?|one|einer?|\d+|half an?|a couple of|two|three|zwei|drei)\s+"
                      r"(?P<unit>minutes?|mins?|hours?|hrs?|minuten|stunden?)\b", text, re.I)
    if not match:
        return None
    n, unit = match.group("n").lower(), match.group("unit").lower()
    count = {"a": 1, "an": 1, "one": 1, "ein": 1, "eine": 1, "einer": 1, "a couple of": 2, "two": 2, "zwei": 2,
             "three": 3, "drei": 3}.get(n, 0.5 if n.startswith("half") else int(n) if n.isdigit() else 1)
    minutes = count * (60 if unit.startswith(("h", "stund")) else 1)
    return int(minutes), match


MONTH_NAMES = {name: n for n, names in enumerate((
    ("january", "jan", "januar", "jän"), ("february", "feb", "februar"), ("march", "mar", "märz", "maerz"),
    ("april", "apr"), ("may", "mai"), ("june", "jun", "juni"), ("july", "jul", "juli"), ("august", "aug"),
    ("september", "sep", "sept"), ("october", "oct", "oktober", "okt"), ("november", "nov"),
    ("december", "dec", "dezember", "dez")), 1) for name in names}
_MONTH = "|".join(sorted(MONTH_NAMES, key=len, reverse=True))


def _find_date(text: str, now: datetime):
    """'20 November', 'the 20th of Nov', 'November 20th', '20.11.', '20.11.2026', 'am 20. November'."""
    year = r"(?:,?\s+(?P<y>\d{4}))?"
    patterns = (rf"\b(?:on |am |the )*(?P<d>\d{{1,2}})(?:st|nd|rd|th|\.)?\s+(?:of\s+)?(?P<mon>{_MONTH})\.?\b{year}",
                rf"\b(?:on )?(?P<mon>{_MONTH})\.?\s+(?:the\s+)?(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\b{year}",
                r"\b(?:on |am )?(?P<d>\d{1,2})\.(?P<m>\d{1,2})\.(?P<y>\d{4})?(?!\d)")
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if not match:
            continue
        month = int(match.group("m")) if "m" in match.groupdict() and match.group("m") else \
            MONTH_NAMES[match.group("mon").lower()]
        try:
            day = now.date().replace(month=month, day=int(match.group("d")))
            if match.group("y"):
                day = day.replace(year=int(match.group("y")))
            elif day < now.date():
                day = day.replace(year=day.year + 1)          # "20 March" said in November: next year
        except ValueError:
            continue
        return day, match
    return None, None


def _find_day(text: str, now: datetime):
    day, match = _find_date(text, now)
    if match:
        return day, match
    match = re.search(r"(?:\b(?:on|am|by|bis)\s+)?\b" + _DAY + r"\b", text, re.I)
    if not match:
        return None, None
    word = match.group("day").lower()
    if match.group("wd"):
        ahead = (WEEKDAYS[match.group("wd").lower()] - now.weekday()) % 7 or 7
        day = now.date() + timedelta(days=ahead)
    elif match.group("n") or match.group("nd"):
        n = match.group("n") or match.group("nd")
        day = now.date() + timedelta(days=int(n) if n.isdigit() else _SMALL[n.lower()])
    elif "after" in word or word == "übermorgen":
        day = now.date() + timedelta(days=2)
    elif word in ("tomorrow", "morgen"):
        day = now.date() + timedelta(days=1)
    else:
        day = now.date()
    return day, match


def _find_time(text: str):
    """'at 10', '10:30', '3pm', 'um 9', '9 Uhr' -> (hour, minute, match). A bare number needs at/um or am/pm/Uhr."""
    for match in re.finditer(r"(?:\b(?P<at>at|um|gegen|around)\s+)?\b" + _TIME + r"(?![\w:])", text, re.I):
        hour, minute = int(match.group("h")), int(match.group("m") or 0)
        marker = (match.group("ap") or "").lower().replace(".", "")
        said_uhr = re.search(r"uhr", match.group(0), re.I)
        dotted = match.group("m") and "." in match.group(0) and not (match.group("at") or said_uhr or marker)
        has_clock = match.group("m") or marker or match.group("at") or said_uhr
        if not has_clock or dotted or minute > 59:            # "Room 2.14" is not a time
            continue
        if marker == "pm" and hour < 12:
            hour += 12
        if marker == "am" and hour == 12:
            hour = 0
        if hour <= 23:
            return hour, minute, match
    return None


class _Span:
    """A found piece of text to cut out, like a regex match."""

    def __init__(self, start: int, end: int):
        self._start, self._end = start, end

    def start(self) -> int:
        return self._start

    def end(self) -> int:
        return self._end


def parse_quick_add(text: str, now: datetime) -> tuple[str, str | None, str | None]:
    """'!pay rent tomorrow at 10' -> title, due, priority. '!' at either end means high.
    Understands today/tomorrow/weekdays/'in 3 days' and times like 10:30, 3pm, 'at 9', 'um 9' (English, German).
    A time without a day means today, or tomorrow if that time has passed."""
    title, priority = text.strip(), None
    if title.startswith("!") or title.endswith("!"):
        title, priority = title.strip("! "), "high"
    relative = find_relative(title)
    if relative:
        rest = re.sub(r"\s+", " ", title[:relative[1].start()] + " " + title[relative[1].end():]).strip(" ,.-")
        if rest:
            when = (now + timedelta(minutes=relative[0])).replace(second=0, microsecond=0)
            return rest, when.isoformat(), priority
    day, day_match = _find_day(title, now)
    clock = _find_time(title)
    if day_match and not clock:                       # "tomorrow 11", "monday 3": a bare hour after the day
        after = re.match(r"\s+(?:at\s+)?(\d{1,2})(?![\d:.]|\s*(?:days?|tage|people|tickets|hours?|min))",
                         title[day_match.end():], re.I)
        if after and 1 <= int(after.group(1)) <= 23:
            hour = int(after.group(1)) + (12 if 1 <= int(after.group(1)) <= 7 else 0)
            clock = (hour, 0, _Span(day_match.end(), day_match.end() + after.end()))
    cuts = [m for m in (day_match, clock[2] if clock else None) if m]
    for match in sorted(cuts, key=lambda m: m.start(), reverse=True):
        title = title[:match.start()] + " " + title[match.end():]
    title = re.sub(r"\s+", " ", title)
    title = re.sub(r"\s*\b(at|on|by|um|am|bis)\s*$", "", title.strip(" ,.-"), flags=re.I).strip(" ,.-")
    if not cuts or not title:
        return text.strip().strip("! ") if not title else title, None, priority
    said = (day_match.groupdict().get("day") or "").lower() if day_match else ""
    if said == "tonight" and not clock:
        clock = (20, 0, None)
    if clock:
        hour, minute = clock[0], clock[1]
        when = datetime.combine(day or now.date(), datetime.min.time()).replace(hour=hour, minute=minute,
                                                                                 tzinfo=now.tzinfo)
        if day is None and when <= now:
            when += timedelta(days=1)
        return title, when.isoformat(), priority
    return title, day.isoformat(), priority


def _ended(item: Item, now: datetime) -> bool:
    """Cached events can outlive their end time while the laptop is offline."""
    return bool(item.end) and _when(item.end, now) <= now


def friendly_time(item: Item, now: datetime) -> str:
    if not item.due:
        return ""
    due = _when(item.due, now)
    days = (due.date() - now.date()).days
    day = {0: "Today", 1: "Tomorrow", -1: "Yesterday"}.get(days) or due.strftime("%a %-d %b")
    if item.source in EVENT_SOURCES and not item.all_day and due <= now:
        return "Happening now"
    if item.all_day:
        return f"Overdue · {day}" if days < 0 else day
    clock = due.strftime("%H:%M")
    return f"Overdue · {day} {clock}" if due < now else f"{day} {clock}"


STATE_VERSION = 1
STATE_KEYS = ("version", "notes", "synced", "overrides", "dismissed", "steps", "places", "snoozed", "window")


def migrate(raw: dict) -> dict:
    """Brings a saved state up to STATE_VERSION. Version 0 is the file before it had a version."""
    version = raw.get("version", 0)
    if version < 1:
        raw = {**raw, "version": 1}           # 0 -> 1: only the version field is new
    return raw


class Store:
    """Everything local: your notes, priority changes, dismissed items and the last synced items."""

    def __init__(self, path: Path | None = None):
        self.path = path or data_dir() / "state.json"
        self.notes: list[Item] = []
        self.synced: dict[str, list[Item]] = {}
        self.overrides: dict[str, str] = {}
        self.dismissed: dict[str, str] = {}
        # Steps are kept by item id, apart from the items, so synced items keep them across syncs.
        self.steps: dict[str, list[dict]] = {}     # id -> [{"text": ..., "done": bool}]
        self.places: dict[str, float] = {}         # id -> position you dragged it to
        self.snoozed: dict[str, str] = {}          # id -> hidden until this time (ISO)
        self.window: dict = {}
        self.extra: dict = {}
        self.file_version = STATE_VERSION
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return
        except ValueError:
            # Keep the damaged file for recovery rather than overwriting it on the next save.
            self.path.replace(self.path.with_suffix(".corrupt"))
            return
        raw = migrate(raw)
        # Sections a newer version added are kept as they are and written back unchanged.
        self.extra = {k: v for k, v in raw.items() if k not in STATE_KEYS}
        self.file_version = raw.get("version", STATE_VERSION)
        self.notes = [Item.from_dict(note) for note in raw.get("notes", [])]
        self.synced = {k: [Item.from_dict(i) for i in v] for k, v in raw.get("synced", {}).items()}
        self.overrides = raw.get("overrides", {})
        self.dismissed = raw.get("dismissed", {})
        self.steps = raw.get("steps", {})
        self.places = raw.get("places", {})
        self.snoozed = raw.get("snoozed", {})
        self.window = raw.get("window", {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = {
            **self.extra,
            "version": max(STATE_VERSION, self.file_version),   # never downgrade a newer file's version
            "notes": [asdict(n) for n in self.notes],
            "synced": {k: [asdict(i) for i in v] for k, v in self.synced.items()},
            "overrides": self.overrides, "dismissed": self.dismissed, "steps": self.steps, "places": self.places,
            "snoozed": self.snoozed, "window": self.window,
        }
        # mkstemp creates the file private, which matters because it holds email subjects.
        handle, temp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-")
        with os.fdopen(handle, "w") as out:
            json.dump(payload, out, indent=1)
        os.replace(temp, self.path)

    def snapshot(self) -> str:
        """Everything you can change by hand, as text: notes, priorities, ticked items and steps.
        Synced items and window layout are left out, so undo never fights with a sync."""
        return json.dumps({"notes": [asdict(n) for n in self.notes], "overrides": self.overrides,
                           "dismissed": self.dismissed, "steps": self.steps, "places": self.places,
                           "snoozed": self.snoozed}, sort_keys=True)

    def restore(self, snapshot: str) -> None:
        raw = json.loads(snapshot)
        self.notes = [Item.from_dict(n) for n in raw["notes"]]
        self.overrides, self.dismissed, self.steps = raw["overrides"], raw["dismissed"], raw["steps"]
        self.places = raw.get("places", {})
        self.snoozed = raw.get("snoozed", {})

    def snooze(self, item: Item, until: datetime) -> None:
        """Hides an item until a time. Your own notes move their due time instead."""
        if item.source == "note":
            for note in self.notes:
                if note.id == item.id:
                    note.due = until.isoformat()
            return
        self.snoozed[item.id] = until.isoformat()

    def add_note(self, text: str, now: datetime) -> Item | None:
        title, due, priority = parse_quick_add(text, now)
        if not title:
            return None
        note = Item(id=f"note:{uuid.uuid4().hex[:12]}", source="note", title=title, due=due)
        self.notes.append(note)
        if priority:
            self.overrides[note.id] = priority
        return note

    def add_step(self, item_id: str, text: str) -> bool:
        text = text.strip()
        if not text:
            return False
        self.steps.setdefault(item_id, []).append({"text": text, "done": False})
        return True

    def toggle_step(self, item_id: str, index: int) -> None:
        step = self.steps[item_id][index]
        step["done"] = not step["done"]

    def edit_step(self, item_id: str, index: int, text: str) -> None:
        if text.strip():
            self.steps[item_id][index]["text"] = text.strip()

    def remove_step(self, item_id: str, index: int) -> None:
        del self.steps[item_id][index]
        if not self.steps[item_id]:
            del self.steps[item_id]

    def clear_done_steps(self, item_id: str) -> None:
        left = [s for s in self.steps.get(item_id, []) if not s["done"]]
        if left:
            self.steps[item_id] = left
        else:
            self.steps.pop(item_id, None)

    def step_progress(self, item_id: str) -> str:
        steps = self.steps.get(item_id, [])
        return f"{sum(s['done'] for s in steps)}/{len(steps)} steps" if steps else ""

    def visible(self, now: datetime) -> list[Item]:
        items = list(self.notes)
        for group in self.synced.values():
            items.extend(i for i in group if i.id not in self.dismissed and not _ended(i, now))
        asleep = {k for k, until in self.snoozed.items() if _when(until, now) > now}
        return [i for i in items if i.id not in asleep]

    def finish(self, item: Item, now: datetime) -> Item:
        """Tick an item off. Notes are removed; synced items are only hidden on this laptop."""
        if item.source == "note":
            self.notes = [n for n in self.notes if n.id != item.id]
        else:
            self.dismissed[item.id] = now.isoformat()
        return item

    def nest(self, child: Item, parent_id: str, now: datetime) -> bool:
        """Makes `child` a step of another item. The step keeps the child's link and remembers the
        child, so it can be made a separate item again. The child's own steps come along."""
        if child.id == parent_id:
            return False
        step = {"text": child.title, "done": False, "from": asdict(child)}
        if child.url:
            step["url"] = child.url
        steps = self.steps.setdefault(parent_id, [])
        steps.append(step)
        steps.extend(self.steps.pop(child.id, []))
        self.overrides.pop(child.id, None)
        self.places.pop(child.id, None)
        self.finish(child, now)
        return True

    def unnest(self, parent_id: str, index: int, now: datetime) -> Item:
        """Takes a step out again as an item of its own: the original item if it came from one,
        otherwise a new note with the step's text and link."""
        step = self.steps[parent_id][index]
        self.remove_step(parent_id, index)
        origin = step.get("from")
        if origin and origin["source"] != "note" and origin["id"] in self.dismissed:
            self.dismissed.pop(origin["id"])        # the synced item shows again
            return Item(**origin)
        if origin and origin["source"] == "note":
            note = Item(**origin)
        else:
            note = Item(id=f"note:{uuid.uuid4().hex[:12]}", source="note", title=step["text"],
                        url=step.get("url", ""))
        self.notes.append(note)
        return note

    def undo_finish(self, item: Item) -> None:
        if item.source == "note":
            self.notes.append(item)
        else:
            self.dismissed.pop(item.id, None)

    def prune(self, now: datetime, keep_days: int = 60) -> None:
        cutoff = now - timedelta(days=keep_days)
        nested = {s["from"]["id"] for steps in self.steps.values() for s in steps if s.get("from")}
        # Items turned into steps stay hidden for as long as the step exists.
        self.dismissed = {k: v for k, v in self.dismissed.items()
                          if k in nested or datetime.fromisoformat(v) > cutoff}
        live = {i.id for i in self.visible(now)}
        self.overrides = {k: v for k, v in self.overrides.items() if k in live}
        self.steps = {k: v for k, v in self.steps.items() if k in live}
        self.places = {k: v for k, v in self.places.items() if k in live}


def ticked_claude(snapshot: dict) -> set[str]:
    """Claude follow-ups that count as done in a snapshot: ticked in the list, or ticked as a step.
    One that was only moved under another item as an unticked step is not done."""
    nested = {s["from"]["id"]: s["done"] for steps in snapshot.get("steps", {}).values() for s in steps
              if s.get("from", {}).get("source") == "claude"}
    done = {k for k in snapshot.get("dismissed", {}) if k.startswith("claude:") and not (k in nested and not nested[k])}
    return done | {k for k, ticked in nested.items() if ticked}


def search(entries: list[tuple[Item, int]], query: str, steps: dict, labels: dict | None = None
           ) -> list[tuple[Item, int]]:
    """Items whose title, details, source or steps contain every word of the query (any case),
    in the order they came in."""
    words = query.lower().split()
    if not words:
        return []
    found = []
    for item, value in entries:
        text = " ".join([item.title, item.detail, (labels or {}).get(item.source, item.source),
                         *(step["text"] for step in steps.get(item.id, []))]).lower()
        if all(word in text for word in words):
            found.append((item, value))
    return found
