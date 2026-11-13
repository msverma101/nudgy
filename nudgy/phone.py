"""Shares what needs you now with the Nudgy pet on your phone.

The laptop does all the reading and ranking. After each update it writes a small file, nudgy-now.json,
into a folder you choose, such as a Syncthing folder that is shared with your phone. The Android app
reads that file and shows the same mood and items. Nothing is sent anywhere by Nudgy itself.

Off until you choose a folder: `nudgy phone ~/Sync/nudgy` (and `nudgy phone off`).
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from .model import NOW, SOON, Item, bucket, config_dir, friendly_time

FILE = "nudgy-now.json"
VERSION = 1
MAX_NOW, MAX_SOON = 8, 6
HEARTBEAT = timedelta(hours=1)    # rewritten at least this often, so the phone knows the laptop is on


def settings_path() -> Path:
    return config_dir() / "phone.json"


def folder() -> Path | None:
    try:
        saved = json.loads(settings_path().read_text()).get("folder")
    except (FileNotFoundError, ValueError):
        return None
    return Path(saved).expanduser() if saved else None


def set_folder(path: str | None) -> None:
    settings_path().parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    settings_path().write_text(json.dumps({"folder": path}) + "\n")


def _entry(item: Item, moment: datetime) -> dict:
    return {"title": item.title[:140], "when": friendly_time(item, moment), "source": item.source,
            "url": item.url if item.url.startswith(("https://", "http://")) else ""}


def summary(scored: list[tuple[Item, int]], moment: datetime) -> dict:
    now_items = [(i, v) for i, v in scored if bucket(v) == NOW]
    soon = [i for i, v in scored if bucket(v) == SOON]
    return {
        "version": VERSION,
        "pressing": len(now_items),
        "alarm": any(v >= 90 for _, v in now_items),
        "now": [_entry(i, moment) for i, _ in now_items[:MAX_NOW]],
        "soon": [_entry(i, moment) for i in soon[:MAX_SOON]],
    }


def export(scored: list[tuple[Item, int]], moment: datetime, target: Path | None = None) -> bool:
    """Writes the file when its content changed. True when written."""
    target = target or folder()
    if target is None:
        return False
    data = summary(scored, moment)
    path = target / FILE
    try:
        old = json.loads(path.read_text())
        written = datetime.fromisoformat(old.pop("updated", "1970-01-01T00:00:00+00:00"))
        if old == data and moment - written < HEARTBEAT:
            return False                  # unchanged: don't wake the sync tool for nothing
    except (FileNotFoundError, ValueError):
        pass
    data["updated"] = moment.isoformat(timespec="seconds")
    target.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=target, prefix=".nudgy-now.", suffix=".tmp")
    with os.fdopen(handle, "w") as out:
        json.dump(data, out, ensure_ascii=False, indent=1)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    return True
