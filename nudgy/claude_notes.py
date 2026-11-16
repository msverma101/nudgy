"""Open follow-ups from Claude Code sessions, read from the notes Claude keeps for you.

`Open Items.md` holds unfinished tasks per project and one "Session handoffs" entry per session;
`Application Deadlines.md` holds dated deadlines. Every unticked `- [ ]` line becomes an item.
Ticking it in Nudgy ticks it in the file, and undo unticks it again.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import urllib.parse
from datetime import date, datetime, timedelta
from pathlib import Path

from .model import Item

SKIP_SECTIONS = {"missed"}
HANDOFFS = "session handoffs"
MONTHS = {m: n for n, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
# "13 Nov", "Fri 13 Nov", "21–22 Nov", "25 Jan 2027", "17-19 Mar 2027"
DATE = re.compile(r"\b(\d{1,2})(?:\s*[–-]\s*\d{1,2})?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
                  r"(?:\s+(20\d\d))?\b", re.I)
# Dates that describe the past, not a deadline: "added 6 Nov", "As of 11 Nov", "on 7 Nov", "Checked 6 Nov".
PAST_CONTEXT = re.compile(r"\b(added|as of|since|checked|on|done|works,|pushed|fixed|failed|tried|sent|set|"
                          r"updated|built|tested|closed|was|last|linked|signed in|found|noted|confirmed|"
                          r"asked|reported|seen)\s*$", re.I)
URGENT = re.compile(r"\burgent\b", re.I)
RESUME = re.compile(r"`cd (/[^`&]+?) && claude --resume ([0-9a-f-]{36})`")
LINK = re.compile(r"https://[^\s)`>\]]+")


def _id(path: Path, section: str, text: str) -> str:
    return "claude:" + hashlib.sha1(f"{path.name}|{section}|{text}".encode()).hexdigest()[:14]


def _plain(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def _dates(text: str, today: date) -> list[date]:
    found = []
    for match in DATE.finditer(text):
        if PAST_CONTEXT.search(text[:match.start()][-12:]):
            continue
        if text[:match.start()].endswith("(") and re.match(r"\s*[:)]", text[match.end():]):
            continue                    # "(11 Nov: …)" or "(11 Nov)" is when a note was written
        day, month = int(match.group(1)), MONTHS[match.group(2).lower()[:3]]
        year = int(match.group(3)) if match.group(3) else today.year
        try:
            when = date(year, month, day)
        except ValueError:
            continue
        if not match.group(3) and when < today - timedelta(days=60):
            when = when.replace(year=year + 1)       # "25 Jan" written in November means next year
        found.append(when)
    return found


def _due(text: str, today: date, deadline_file: bool) -> str | None:
    if deadline_file:
        # In the deadlines file the bold date at the start is the deadline, even once it has passed.
        lead = re.match(r"\*\*(.+?)\*\*", text)
        dates = _dates(lead.group(1), today) if lead else []
        if dates:
            return dates[0].isoformat()
    upcoming = [d for d in _dates(text, today) if d >= today]
    return min(upcoming).isoformat() if upcoming else None


def _vault_url(path: Path) -> str:
    """Obsidian if the notes folder is one of its vaults, otherwise the file itself."""
    config = Path.home() / ".config/obsidian/obsidian.json"
    try:
        vaults = [Path(v["path"]) for v in json.loads(config.read_text()).get("vaults", {}).values()]
    except (OSError, ValueError, KeyError):
        vaults = []
    if any(path.is_relative_to(v) for v in vaults):
        return "obsidian://open?path=" + urllib.parse.quote(str(path))
    return path.as_uri()


def folder() -> Path | None:
    """The chosen notes folder (settings "claude_notes.folder", or $NUDGY_NOTES). None: this source is off."""
    from . import settings as prefs
    chosen = os.environ.get("NUDGY_NOTES") or prefs.section("claude_notes")["folder"]
    return Path(chosen).expanduser() if chosen else None


def paths(base: Path | None = None) -> list[Path]:
    """The files read: the ones named in settings, or every .md file in the folder."""
    from . import settings as prefs
    base = base or folder()
    if base is None:
        return []
    names = prefs.section("claude_notes")["files"]
    return [base / name for name in names] if names else sorted(base.glob("*.md"))


def is_deadlines(path: Path) -> bool:
    """Files named like "Deadlines.md": their dates are hard deadlines, so overdue items stay overdue."""
    return "deadline" in path.name.lower()


def read(now: datetime, base: Path | None = None) -> list[Item]:
    today = now.date()
    items = []
    for path in paths(base):
        try:
            lines = path.read_text().splitlines()
        except FileNotFoundError:
            continue
        file_url = _vault_url(path)
        section = ""
        for number, line in enumerate(lines):
            if line.startswith("## "):
                section = re.sub(r"\s*\((added|check)[^)]*\)\s*$", "", line[3:].strip(), flags=re.I)
                continue
            if not line.startswith("- [ ] ") or section.lower() in SKIP_SECTIONS:
                continue
            text = line[6:].strip()
            item_id = _id(path, section, text)
            if section.lower() == HANDOFFS:
                resume = RESUME.search(text)
                project = re.search(r"\*\*(.+?)\*\*", text)
                status = next((_plain(line.split("**Status:**", 1)[1]) for line in lines[number + 1:number + 4]
                               if "**Status:**" in line), "")
                session = _plain(project.group(1)) if project else "session"
                when, _, what = session.partition(" · ")
                title = f"Resume: {what} ({when.rsplit(' ', 1)[0]})" if what else f"Resume: {session}"
                items.append(Item(id=item_id, source="claude", title=title,
                                  detail=status,
                                  url=f"resume:{resume.group(1)}|{resume.group(2)}" if resume else file_url,
                                  tags=["handoff"]))
                continue
            plain = _plain(text)
            link = LINK.search(text)
            tags = ["urgent"] if URGENT.search(text) else []
            items.append(Item(id=item_id, source="claude", title=plain, detail=section,
                              due=_due(text, today, is_deadlines(path)),
                              url=link.group(0).rstrip(".,") if link else file_url, tags=tags))
    return items


def set_ticked(item_id: str, done: bool, base: Path | None = None) -> bool:
    """Ticks or unticks the line behind an item. Returns False if the line can't be found."""
    for path in paths(base):
        try:
            lines = path.read_text().split("\n")
        except FileNotFoundError:
            continue
        section = ""
        for number, line in enumerate(lines):
            if line.startswith("## "):
                section = re.sub(r"\s*\((added|check)[^)]*\)\s*$", "", line[3:].strip(), flags=re.I)
            elif line.startswith(("- [ ] ", "- [x] ")) and _id(path, section, line[6:].strip()) == item_id:
                lines[number] = ("- [x] " if done else "- [ ] ") + line[6:]
                temp = path.with_suffix(".nudgy-tmp")
                temp.write_text("\n".join(lines))
                os.replace(temp, path)
                return True
    return False


def resume(url: str) -> None:
    """Opens a terminal that resumes the Claude Code session."""
    folder, session = url[len("resume:"):].split("|")
    if not re.fullmatch(r"[0-9a-f-]{36}", session) or not Path(folder).is_dir():
        raise ValueError("That handoff has no valid resume command")
    claude = shutil.which("claude") or str(Path.home() / ".local/bin/claude")
    scope = ["systemd-run", "--user", "--scope", "--collect", "--quiet"] if shutil.which("systemd-run") else []
    subprocess.Popen([*scope, "gnome-terminal", f"--working-directory={folder}", "--",
                      "bash", "-lc", f"'{claude}' --resume {session}; exec bash"])
