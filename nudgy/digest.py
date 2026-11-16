"""Turns long jotted notes into concrete steps, using Claude once an hour (or when you press ↻).

Notes you jot at events (in Keep, which become Google Tasks, or as Nudgy notes) are often a pile
of names, links and ideas without anything to do. For each new one, Claude suggests 2 to 5 steps you
can actually do: try, read, sign up, call, book. They appear as steps under the item, marked ✦, with
the link when the note had one. Remove any you don't want with ×. Each note is processed once; if you
edit it, it is processed again.

Settings (optional) in ~/.config/nudgy/digest.json: {"enabled": true, "model": "haiku"}.
"""
from __future__ import annotations

from typing import Any
import hashlib
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

from .model import Item, config_dir, data_dir, quiet_claude_env

EVERY = 3600
SOURCES = ("task", "keep", "note")
MAX_PER_CALL = 8
MARK = "✦ "
SYSTEM = (
    "You turn messy notes that someone jotted at events and talks into concrete next actions. "
    "For each note give 2 to 5 actions they can actually do or test soon: start with a verb (Try, Read, "
    "Sign up, Watch, Install, Call, Book, Ask, Compare), at most 12 words, specific (name the tool, "
    "paper, person or site), and doable in one sitting. Include personal errands found in the note "
    "(calls, appointments, purchases). Skip vague items and opinions. If the note has a link that an "
    "action needs, put it in url. Keep names and non-English words as written. Answer with JSON only: "
    '{"notes": [{"id": "...", "steps": [{"text": "...", "url": ""}]}]}')


def state_path() -> Path:
    return data_dir() / "digest.json"


def _state() -> dict:
    try:
        return json.loads(state_path().read_text())
    except (FileNotFoundError, ValueError):
        return {"last_run": 0, "done": {}, "tokens": 0}


def _save(state: dict) -> None:
    state_path().parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_path().write_text(json.dumps(state))


def settings() -> dict:
    try:
        saved = json.loads((config_dir() / "digest.json").read_text())
    except (FileNotFoundError, ValueError):
        saved = {}
    return {"enabled": False, "model": "haiku", **saved}     # off: it sends your notes to Claude


def due() -> bool:
    return settings()["enabled"] and time.time() - _state().get("last_run", 0) >= EVERY


def _fingerprint(item: Item) -> str:
    return hashlib.sha1(f"{item.title}\n{item.detail}".encode()).hexdigest()[:12]


def is_jotted(text: str) -> bool:
    """Long, or a pile of fragments: the kind of note that needs turning into actions."""
    pieces = len(re.findall(r" - |, |\n| {2,}|https?://", text))
    return len(text) >= 160 or (len(text) >= 90 and pieces >= 4)


def candidates(items: list[Item], steps: dict, done: dict) -> list[Item]:
    found = []
    for item in items:
        if item.source not in SOURCES or steps.get(item.id):     # already has steps: yours to manage
            continue
        if done.get(item.id) == _fingerprint(item):
            continue
        if is_jotted(item.title):
            found.append(item)
    return found


def parse(text: str, wanted: set[str]) -> dict[str, list[dict]]:
    match = re.search(r"\{.*\}", text or "", re.S)
    try:
        notes = json.loads(match.group(0)).get("notes", []) if match else []
    except (ValueError, AttributeError):
        return {}
    result = {}
    for note in notes:
        if not isinstance(note, dict) or note.get("id") not in wanted:
            continue
        steps = []
        for step in note.get("steps", [])[:5]:
            text = str(step.get("text", "")).strip() if isinstance(step, dict) else ""
            if not text:
                continue
            entry = {"text": MARK + text[:120], "done": False, "ai": True}
            url = str(step.get("url", "")).strip()
            if url.startswith("https://"):
                entry["url"] = url
            steps.append(entry)
        if steps:
            result[note["id"]] = steps
    return result


def ask_claude(notes: list[Item], model: str) -> tuple[str, int]:
    """Claude's answer text and the tokens used. Uses your Claude Code login, no tools."""
    payload = "\n\n".join(f"id: {n.id}\nnote: {n.title}" for n in notes)
    claude = shutil.which("claude") or str(Path.home() / ".local/bin/claude")
    command = [claude, "-p", payload, "--model", model, "--output-format", "json", "--tools", "",
               "--strict-mcp-config", "--no-session-persistence", "--setting-sources", "",
               "--system-prompt", SYSTEM]
    result = subprocess.run(command, capture_output=True, text=True, timeout=180, cwd=str(data_dir()),
                            env=quiet_claude_env())
    answer = json.loads(result.stdout)
    usage = answer.get("usage", {})
    tokens = sum(usage.get(k, 0) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                                           "cache_read_input_tokens"))
    if answer.get("is_error"):
        raise RuntimeError(str(answer.get("result", "Claude failed"))[:120])
    return answer.get("result", ""), tokens


def run(items: list[Item], steps: dict, ask=ask_claude) -> dict:
    """Processes new jotted notes. Returns {"steps": {item_id: [step, ...]}, "error": str, "tokens": n}."""
    config = settings()
    if not config["enabled"]:
        return {}
    state = _state()
    todo = candidates(items, steps, state.get("done", {}))
    result: dict[str, Any] = {"steps": {}, "error": "", "tokens": 0}
    for start in range(0, len(todo), MAX_PER_CALL):
        batch = todo[start:start + MAX_PER_CALL]
        try:
            text, tokens = ask(batch, config["model"])
        except (OSError, subprocess.TimeoutExpired, ValueError, RuntimeError) as error:
            result["error"] = f"Claude could not process notes ({error})"[:160]
            break
        result["tokens"] += tokens
        result["steps"].update(parse(text, {n.id for n in batch}))
        for note in batch:                    # processed, even if Claude found nothing to do
            state.setdefault("done", {})[note.id] = _fingerprint(note)
    if not result["error"]:
        state["last_run"] = time.time()
    state["tokens"] = state.get("tokens", 0) + result["tokens"]
    _save(state)
    return result
