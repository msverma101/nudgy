"""All of Nudgy's preferences, in one file: ~/.config/nudgy/settings.json.

DEFAULTS below is the schema: every section, every key, its default and (by its type) what it may hold.
The file only stores what you changed. A value of the wrong type is ignored with a warning, so a typo
never breaks a feature. Sign-ins and other secrets are not settings: they stay in their own private
files (token.json, google-client.json, luma.json, keep-token.json).

Older versions kept one file per feature (focus.json, voice.json, digest.json, discover.json, phone.json,
google.json). The first time this runs they are merged in here and renamed to *.json.migrated.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from .model import config_dir

log = logging.getLogger(__name__)

DEFAULTS: dict[str, dict[str, Any]] = {
    # How items are ranked and how far ahead sources look.
    "ranking": {
        # Words in a mail or chat that make it urgent (whole words, any case).
        "urgent_words": ["urgent", "asap", "deadline", "action required", "final notice", "overdue"],
        "calendar_days": 7,           # calendar events this many days ahead
        "mail_days": 14,              # unread mail from the last this-many days
        "events_days": 14,            # Luma events this many days ahead
    },
    "google": {
        "skip_calendars": [],         # calendar names to leave out, e.g. one that only holds alarms
    },
    # Markdown checklists, such as the notes Claude Code sessions keep. Off until a folder is chosen.
    "claude_notes": {
        "folder": "",                 # e.g. "~/Documents/Notes"
        "files": [],                  # file names in that folder; empty = every .md file in it
    },
    # The focus check: judges the active window's title against what's urgent. Sends titles to a model.
    "focus": {
        "enabled": False,
        "interval_seconds": 3600,     # how often the active window is read: once an hour
        "same_window_samples": 1,     # how many reads in a row on the same window before judging
        "idle_minutes": 5,            # no judging while you're away from the keyboard
        "work_apps": ["gnome-terminal", "terminal", "code", "codium", "cursor", "blender", "obsidian",
                      "godot", "kitty", "alacritty", "jetbrains", "pycharm", "libreoffice", "gimp", "inkscape",
                      "kdenlive", "davinci", "zotero", "evince", "okular", "nudgy"],
        "distractions": ["youtube", "instagram", "netflix", "reddit", "twitter", " x.com", "/ x", "tiktok",
                         "facebook", "twitch", "9gag", "prime video", "disney+", "spotify"],
        "local_model": "qwen3:8b",
        "ollama_url": "http://127.0.0.1:11434",
        "lookups": True,              # let the local model look up one unknown term on Wikipedia
        "claude": True,
        "claude_model": "haiku",
        "claude_daily_cap": 20,
        "unsure_below": 0.7,          # a verdict less confident than this counts as unsure
        # With hourly checks every drifting check steps up once: side-eye, then sulk, then its back.
        "steps_minutes": [50, 50],
        "steps_minutes_overdue": [50, 50],
        "log_days": 7,
        "work_topics": [],            # your ongoing work: anything clearly about these counts as on task
    },
    # Talking to the pet. Local models; see voice.py.
    "voice": {
        "enabled": False,
        "model": "qwen3:4b",
        "ollama_url": "http://localhost:11434",
        "whisper": "base",            # "small" hears better but is ~3x slower on a laptop CPU
        "speak": True,
        "max_seconds": 20,
        "claude": False,              # allow Claude for drafting replies and breaking items into steps
        "microphone": "auto",         # auto: your Bluetooth headset's mic while you wear it, else the laptop's
        "voices": {"en": "en_US-hfc_female-medium", "de": "de_DE-thorsten-medium"},
    },
    # Claude turns long jotted notes into steps. Sends those notes to Claude.
    "digest": {
        "enabled": False,
        "model": "haiku",
    },
    # Public events that fit you. See discover.py and examples/discover.json.
    "discover": {
        "enabled": False,
        "home": "",                   # your city; events in and near it rank first
        "near_km": 500,               # "near" means within this distance of home
        "luma_calendars": {},         # {"cal-…": "name"}, from a Luma calendar's Subscribe link
        "organiser_boost": {},        # {"organiser name": points}
        "luma_cities": [],
        "meetup_groups": [],
        "rss": [],
        "watchlist": [],              # events you add by hand: {"title", "start", "end", "where", "url", "about"}
        # What you're interested in: [{"label": "AI safety", "words": ["alignment", "interpretab"], "points": 20}]
        "topics": [],
        "big_names": [],              # organisations whose events get extra points
    },
    # Sharing the list with the Android app through a folder (e.g. a Syncthing folder).
    "phone": {
        "folder": "",
    },
}

OLD_FILES = {"focus.json": "focus", "voice.json": "voice", "digest.json": "digest", "discover.json": "discover",
             "phone.json": "phone", "google.json": "google"}

_cache: dict[str, Any] = {"mtime": None, "data": None}


def path() -> Path:
    return config_dir() / "settings.json"


def _read() -> dict:
    try:
        raw = json.loads(path().read_text())
        return raw if isinstance(raw, dict) else {}
    except FileNotFoundError:
        return {}
    except ValueError as error:
        log.warning("settings.json is not valid JSON (%s); using defaults until it's fixed", error)
        return {}


def _write(data: dict) -> None:
    path().parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle, temporary = tempfile.mkstemp(dir=path().parent, prefix=".settings-")
    with os.fdopen(handle, "w") as out:
        json.dump(data, out, indent=1, ensure_ascii=False)
        out.write("\n")
    os.replace(temporary, path())
    _cache["mtime"] = None


def migrate() -> bool:
    """Merges the old per-feature files into settings.json once. True if anything was migrated."""
    if path().exists():
        return False
    data: dict[str, dict] = {}
    found = []
    for name, section in OLD_FILES.items():
        old = config_dir() / name
        try:
            saved = json.loads(old.read_text())
        except FileNotFoundError:
            continue
        except ValueError:
            log.warning("%s is not valid JSON; not migrated", name)
            continue
        if not isinstance(saved, dict):
            continue
        if section == "discover":
            saved = {"enabled": True, **saved}          # having discover.json used to switch it on
        data[section] = saved
        found.append(old)
    if not found:
        return False
    _write(data)
    for old in found:
        old.replace(old.with_name(old.name + ".migrated"))
    log.info("moved %s into settings.json", ", ".join(o.name for o in found))
    return True


def _checked(section: str, saved: dict) -> dict:
    """Saved values whose type matches the default's; others are dropped with a warning."""
    defaults = DEFAULTS[section]
    good = {}
    for key, value in saved.items():
        if key not in defaults:
            good[key] = value                         # unknown keys are kept (a newer version may use them)
            continue
        expected = type(defaults[key])
        if expected is bool:
            ok = isinstance(value, bool)
        elif expected in (int, float):                # whole numbers are fine where decimals are expected
            ok = isinstance(value, (int, float) if expected is float else int) and not isinstance(value, bool)
        else:
            ok = isinstance(value, expected)
        if ok:
            good[key] = value
        else:
            log.warning("settings: %s.%s should be %s, not %r; using the default",
                        section, key, expected.__name__, value)
    return good


def _current() -> dict[str, dict[str, Any]]:
    """Every section, defaults filled in. Cached until the file changes. Don't modify the result."""
    migrate()
    try:
        info = path().stat()
        # The file's identity, size and time: a timestamp alone can repeat within a few milliseconds.
        mtime: Any = (str(path()), info.st_ino, info.st_size, info.st_mtime_ns)
    except FileNotFoundError:
        mtime = (str(path()), 0)
    if _cache["mtime"] != mtime or _cache["data"] is None:
        saved = _read()
        merged = {}
        for section, defaults in DEFAULTS.items():
            mine = saved.get(section, {})
            mine = _checked(section, mine if isinstance(mine, dict) else {})
            values = copy.deepcopy(defaults)
            for key, value in mine.items():
                if isinstance(values.get(key), dict) and isinstance(value, dict) and section == "voice":
                    values[key] = {**values[key], **value}   # e.g. add a voice, keep the others
                else:
                    values[key] = value
            merged[section] = values
        _cache.update(mtime=mtime, data=merged)
    return _cache["data"]


def load() -> dict[str, dict[str, Any]]:
    """Every section, defaults filled in, as a copy you may change."""
    return copy.deepcopy(_current())


def section(name: str) -> dict[str, Any]:
    return copy.deepcopy(_current()[name])


def update(name: str, changes: dict[str, Any]) -> None:
    """Saves changes to one section; keeps everything else in the file as it is."""
    migrate()
    data = _read()
    current = data.get(name, {}) if isinstance(data.get(name), dict) else {}
    data[name] = {**current, **changes}
    _write(data)
