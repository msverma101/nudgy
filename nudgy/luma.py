"""Luma events from your personal iCal feed (Luma > Settings > Calendar Syncing).

Luma's API is only for hosts on a paid plan, but the feed lists events you host, registered for,
are waitlisted or pending for, or were invited to. The feed link is private, like a password,
so it is stored with owner-only permissions.
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .model import Item, config_dir

DAYS_AHEAD = 14


def _zone(name: str, fallback):
    """A named time zone; the given fallback if this computer has no time-zone data for it."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return fallback


def feed_path() -> Path:
    return config_dir() / "luma.json"


def feed_url() -> str | None:
    try:
        return json.loads(feed_path().read_text()).get("url")
    except (FileNotFoundError, ValueError):
        return None


def save_feed(url: str) -> None:
    url = url.strip()
    if url.startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("Paste the https:// or webcal:// link from Luma's Calendar Syncing settings.")
    path = feed_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as out:
        json.dump({"url": url}, out)


def forget_feed() -> None:
    feed_path().unlink(missing_ok=True)


# ---- minimal iCalendar reading -----------------------------------------------------------------
def _unescape(value: str) -> str:
    return re.sub(r"\\([,;\\nN])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), value)


def parse_ics(text: str) -> list[dict]:
    """VEVENTs as {NAME: (params, value)}. Handles folded lines; enough for Luma's feed."""
    lines = re.sub(r"\r?\n[ \t]", "", text).splitlines()
    events: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in lines:
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT" and current is not None:
            events.append(current)
            current = None
        elif current is not None and ":" in line:
            head, value = line.split(":", 1)
            name, *raw_params = head.split(";")
            params = dict(p.split("=", 1) for p in raw_params if "=" in p)
            current.setdefault(name.upper(), (params, value))
    return events


def _moment(field: tuple[dict, str] | None, local) -> datetime | date | None:
    if not field:
        return None
    params, value = field
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        return datetime.strptime(value[:8], "%Y%m%d").date()
    stamp = datetime.strptime(value.rstrip("Z")[:15], "%Y%m%dT%H%M%S")
    if value.endswith("Z"):
        return stamp.replace(tzinfo=timezone.utc).astimezone(local)
    zone = _zone(params["TZID"].strip('"'), local) if "TZID" in params else local
    return stamp.replace(tzinfo=zone).astimezone(local)


def luma_items(text: str, now: datetime, days: int = DAYS_AHEAD) -> list[Item]:
    items = []
    horizon = now + timedelta(days=days)
    for event in parse_ics(text):
        start, end = _moment(event.get("DTSTART"), now.tzinfo), _moment(event.get("DTEND"), now.tzinfo)
        if start is None or event.get("STATUS", ({}, ""))[1].upper() == "CANCELLED":
            continue
        all_day = not isinstance(start, datetime)
        start_cmp = datetime.combine(start, datetime.min.time(), now.tzinfo) if all_day else start
        end_cmp = (datetime.combine(end, datetime.min.time(), now.tzinfo) if end and all_day else end) or start_cmp
        if end_cmp <= now or start_cmp > horizon:
            continue
        title = _unescape(event.get("SUMMARY", ({}, "Luma event"))[1])
        location = _unescape(event.get("LOCATION", ({}, ""))[1])
        description = _unescape(event.get("DESCRIPTION", ({}, ""))[1])
        url = event.get("URL", ({}, ""))[1]
        if not url:
            found = re.search(r"https://(?:lu\.ma|luma\.com)/[\w-]+", description + " " + location)
            url = found.group(0) if found else ""
        status = event.get("STATUS", ({}, ""))[1].upper()
        detail = "Pending or waitlisted" if status == "TENTATIVE" else (
            "" if location.startswith("http") else location.split(",")[0])
        uid = event.get("UID", ({}, title + str(start)))[1]
        items.append(Item(id=f"luma:{uid}", source="luma", title=title, detail=detail,
                          due=start.isoformat(), end=end.isoformat() if end else None,
                          url=url if url.startswith("https://") else "", tags=["event"]))
    return items


def fetch_luma(now: datetime, days: int = DAYS_AHEAD) -> list[Item] | None:
    url = feed_url()
    if not url:
        return None
    request = urllib.request.Request(url, headers={"User-Agent": "Nudgy calendar reader"})
    with urllib.request.urlopen(request, timeout=30) as response:
        text = response.read().decode("utf-8", "replace")
    if "BEGIN:VCALENDAR" not in text:
        raise ValueError("Luma link did not return a calendar. Paste it again via 👤.")
    return luma_items(text, now, days)


def _key(item: Item) -> tuple[str, str]:
    when = item.due or ""
    try:
        parsed = datetime.fromisoformat(when)
        if parsed.tzinfo:            # same moment written in different time zones must match
            when = parsed.astimezone(timezone.utc).isoformat()
    except ValueError:
        pass
    return (re.sub(r"\W+", "", item.title.lower()), when[:16])


def drop_duplicates(luma: list[Item], calendar: list[Item]) -> list[Item]:
    """Luma also emails calendar invites, so the same event is often in Google Calendar already."""
    seen = {_key(event) for event in calendar}
    urls = {u.group(0).rstrip("/") for event in calendar
            for u in [re.search(r"https://(?:lu\.ma|luma\.com)/[\w-]+", f"{event.url} {event.detail}")] if u}
    return [i for i in luma if _key(i) not in seen and i.url.rstrip("/") not in urls]
