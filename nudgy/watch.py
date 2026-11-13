"""Focus check: notices when you drift away from what's urgent, and lets the pet react.

Every few minutes it reads the active window's app name and title (no screenshots, no keys). When
something is urgent and the same window has stayed in front for a while, it asks, cheapest first:
  1. rules: words from your urgent tasks in the title, known work apps, known distractions
  2. a local model through Ollama (qwen3:8b by default), which may look up one unfamiliar term on
     Wikipedia (only that term is sent)
  3. Claude (Haiku, through your Claude Code login), only when the local model is unsure, at most
     a set number of times a day
Verdicts are cached per title for an hour. Everything is logged locally in focus-log.jsonl.

Settings live in ~/.config/nudgy/focus.json; any key left out uses the default below.
"""
from __future__ import annotations

import logging
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from .model import config_dir, data_dir, quiet_claude_env

log = logging.getLogger(__name__)

DEFAULTS = {
    "enabled": False,                 # sends window titles to a model; turn on in the app or focus.json
    "interval_seconds": 3600,         # how often the active window is read: once an hour
    "same_window_samples": 1,         # how many reads in a row on the same window before judging
    "idle_minutes": 5,                # no judging while you're away from the keyboard
    "work_apps": ["gnome-terminal", "terminal", "code", "codium", "cursor", "blender", "obsidian",
                  "godot", "kitty", "alacritty", "jetbrains", "pycharm", "libreoffice", "gimp", "inkscape",
                  "kdenlive", "davinci", "zotero", "evince", "okular", "nudgy"],
    "distractions": ["youtube", "instagram", "netflix", "reddit", "twitter", " x.com", "/ x", "tiktok",
                     "facebook", "twitch", "9gag", "prime video", "disney+", "spotify"],
    "local_model": "qwen3:8b",
    "ollama_url": "http://127.0.0.1:11434",
    "lookups": True,                  # let the local model look up one unknown term on Wikipedia
    "claude": True,
    "claude_model": "haiku",
    "claude_daily_cap": 20,
    "unsure_below": 0.7,              # a verdict less confident than this counts as unsure
    # With hourly checks every drifting check steps up once: side-eye, then sulk, then its back.
    # (With a shorter interval these minutes make it sulk sooner; overdue steps come faster.)
    "steps_minutes": [50, 50],
    "steps_minutes_overdue": [50, 50],
    "log_days": 7,
    # Your ongoing work: anything clearly about these counts as on task even if no listed task names it.
    "work_topics": [],
}
# Titles that say nothing about what you're doing: no judging at all.
GENERIC = re.compile(r"^\s*((new tab|untitled|mozilla firefox|google chrome|chromium|home|files|"
                     r"desktop|loading…?)\s*[-—–]?\s*)+$", re.I)
STOP = {"with", "from", "that", "this", "your", "have", "about", "into", "then", "they", "their", "what",
        "when", "will", "would", "there", "check", "make", "need", "next", "send", "apply", "email", "reply",
        "today", "tomorrow", "week", "urgent", "event", "meeting", "after", "before", "only", "still"}
SYSTEM = ("You judge whether the user's active window is helping with their urgent tasks or their "
          "ongoing work areas. Tutorials, documentation, tools, papers and research related to a task or "
          "work area count as on task. Entertainment, social feeds, shopping and unrelated browsing count as "
          "digressing. Be calibrated: use confidence above 0.8 only when the title clearly shows it. If the "
          "title gives little evidence, answer unsure with low confidence. "
          "Answer with exactly one JSON object: "
          '{"verdict": "on_task" | "digressing" | "unsure", "confidence": 0.0-1.0, '
          '"task": "the task it relates to or blocks", "reason": "under 15 words", '
          '"lookup": "one unfamiliar name in the title you would need explained, or empty"}')


def config_path() -> Path:
    return config_dir() / "focus.json"


def load_config() -> dict:
    try:
        saved = json.loads(config_path().read_text())
    except (FileNotFoundError, ValueError):
        saved = {}
    return {**DEFAULTS, **saved}


def save_config(changes: dict) -> None:
    try:
        saved = json.loads(config_path().read_text())
    except (FileNotFoundError, ValueError):
        saved = {}
    saved.update(changes)
    config_path().parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    config_path().write_text(json.dumps(saved, indent=1))


@dataclass
class Verdict:
    verdict: str          # on_task | digressing | unsure
    confidence: float
    task: str = ""
    reason: str = ""
    judge: str = ""       # rules | local | local+lookup | claude | cache | you
    lookup: str = ""


def parse_verdict(text: str, judge: str) -> Verdict:
    """Reads a model's answer, tolerating extra text around the JSON."""
    match = re.search(r"\{.*\}", text or "", re.S)
    try:
        raw = json.loads(match.group(0)) if match else {}
    except ValueError:
        raw = {}
    verdict = raw.get("verdict") if raw.get("verdict") in ("on_task", "digressing", "unsure") else "unsure"
    try:
        confidence = max(0.0, min(1.0, float(raw.get("confidence", 0))))
    except (TypeError, ValueError):
        confidence = 0.0
    lookup = str(raw.get("lookup") or "").strip()
    task = str(raw.get("task") or "")
    task = "" if task.strip().lower() in ("none", "n/a", "null", "-") else task
    return Verdict(verdict, confidence, task[:120], str(raw.get("reason") or "")[:160],
                   judge, lookup if 0 < len(lookup) <= 40 else "")


def task_words(tasks: list[tuple[str, str]]) -> dict[str, str]:
    """Distinctive words from task titles, each mapped to its task."""
    words = {}
    for title, _when in tasks:
        for word in re.findall(r"[a-zäöüß][a-zäöüß0-9.+-]{3,}", title.lower()):
            if word not in STOP and word not in words:
                words[word.strip(".-")] = title
    return words


def site_of(app: str, title: str, config: dict) -> str:
    """A short name for what's in front: the distraction if one matches, else the app."""
    low = f"{title} {app}".lower()
    for name in config["distractions"]:
        if name.strip() and name.strip(" /") in low:
            return name.strip(" /.").replace("x.com", "X").title()
    return app or "this"


def rules(app: str, title: str, tasks: list[tuple[str, str]], config: dict) -> Verdict | None:
    low_title, low_app = title.lower(), app.lower()
    distracting = any(name.strip() and name.strip(" /") in f"{low_title} {low_app}" for name in config["distractions"])
    for word, task in task_words(tasks).items():
        if re.search(rf"\b{re.escape(word)}\b", low_title):
            return Verdict("on_task", 0.9, task, f"title mentions “{word}”", "rules")
    if not distracting and any(name in low_app for name in config["work_apps"]):
        return Verdict("on_task", 0.8, "", f"{app} is a work app", "rules")
    return None          # distractions and everything else go to the model: a tutorial can be on task


def _prompt(app: str, title: str, tasks: list[tuple[str, str]], extra: str = "", topics=()) -> str:
    listed = "\n".join(f"{n}. {t}" + (f" ({w})" if w else "") for n, (t, w) in enumerate(tasks[:5], 1))
    text = f'Active window: "{title}" (app: {app})\nUrgent tasks:\n{listed}'
    if topics:
        text += "\nOngoing work areas: " + ", ".join(topics)
    return text + (f"\n\nBackground on a term in the title: {extra}" if extra else "")


def ask_local(app: str, title: str, tasks, config: dict, extra: str = "") -> Verdict | None:
    body = json.dumps({"model": config["local_model"], "stream": False, "think": False, "format": "json",
                       "keep_alive": "2m",            # free the model's ~5 GB soon after the check
                       "options": {"temperature": 0},
                       "messages": [{"role": "system", "content": SYSTEM},
                                    {"role": "user", "content": _prompt(app, title, tasks, extra, config["work_topics"])}]}).encode()
    request = urllib.request.Request(config["ollama_url"].rstrip("/") + "/api/chat", data=body,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            content = json.load(response)["message"]["content"]
    except (OSError, ValueError, KeyError):
        return None                              # Ollama not running or model missing
    return parse_verdict(content, "local")


def wikipedia(term: str) -> str:
    url = "https://en.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(term.replace(" ", "_"))
    request = urllib.request.Request(url, headers={"User-Agent": "Nudgy focus check"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response).get("extract", "")[:500]
    except (OSError, ValueError):
        return ""


def ask_claude(app: str, title: str, tasks, config: dict) -> tuple[Verdict | None, int]:
    """Returns the verdict and the tokens it used."""
    claude = shutil.which("claude") or str(Path.home() / ".local/bin/claude")
    command = [claude, "-p", _prompt(app, title, tasks, topics=config["work_topics"]), "--model", config["claude_model"],
               "--output-format", "json", "--tools", "", "--strict-mcp-config", "--no-session-persistence",
               "--setting-sources", "", "--system-prompt", SYSTEM]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=90, cwd=str(data_dir()),
                                env=quiet_claude_env())
        answer = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None, 0
    usage = answer.get("usage", {})
    tokens = sum(usage.get(k, 0) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                                           "cache_read_input_tokens"))
    return parse_verdict(answer.get("result", ""), "claude"), tokens


class Judge:
    """Runs the rules → local → Claude chain with a cache and a daily Claude cap."""

    def __init__(self, config: dict | None = None, local=ask_local, claude=ask_claude, lookup=wikipedia):
        self.config = config or load_config()
        self.local, self.claude, self.lookup = local, claude, lookup
        self.cache: dict[str, tuple[float, Verdict]] = {}
        self.claude_day, self.claude_calls, self.claude_tokens = "", 0, 0

    def key(self, app: str, title: str, tasks) -> str:
        tops = "|".join(t for t, _ in tasks[:5])
        return hashlib.sha1(f"{app}\n{title}\n{tops}".encode()).hexdigest()

    def allow(self, app: str, title: str, tasks, minutes: int = 60) -> None:
        """You said it is on task: believe that for a while."""
        self.cache[self.key(app, title, tasks)] = (time.time() + minutes * 60,
                                                    Verdict("on_task", 1.0, "", "you said so", "you"))

    def decide(self, app: str, title: str, tasks) -> Verdict:
        key = self.key(app, title, tasks)
        cached = self.cache.get(key)
        if cached and cached[0] > time.time():
            verdict = cached[1]
            return Verdict(verdict.verdict, verdict.confidence, verdict.task, verdict.reason,
                           verdict.judge if verdict.judge == "you" else "cache")
        if GENERIC.match(title) or title.strip().lower() == app.strip().lower():
            return Verdict("unsure", 0.0, "", "the title says nothing about the work", "rules")
        verdict = rules(app, title, tasks, self.config) or self._models(app, title, tasks)
        self.cache[key] = (time.time() + 3600, verdict)
        return verdict

    def _models(self, app: str, title: str, tasks) -> Verdict:
        floor = self.config["unsure_below"]
        verdict = self.local(app, title, tasks, self.config)
        if verdict and (verdict.verdict == "unsure" or verdict.confidence < floor) \
                and verdict.lookup and self.config["lookups"]:
            background = self.lookup(verdict.lookup)
            if background:
                again = self.local(app, title, tasks, self.config, f"{verdict.lookup}: {background}")
                if again:
                    again.judge = "local+lookup"
                    verdict = again
        if verdict and verdict.verdict != "unsure" and verdict.confidence >= floor:
            return verdict
        today = datetime.now().date().isoformat()
        if self.claude_day != today:
            self.claude_day, self.claude_calls = today, 0
        if self.config["claude"] and self.claude_calls < self.config["claude_daily_cap"]:
            self.claude_calls += 1
            answer, tokens = self.claude(app, title, tasks, self.config)
            self.claude_tokens += tokens
            if answer:
                return answer
        return verdict or Verdict("unsure", 0.0, "", "no judge available", "none")


class Escalation:
    """How judgy the pet is: 0 calm, 1 side-eye, 2 sulk, 3 back turned. The level follows how long
    you have been drifting, so "Back to work" without going back gets you a sulk next time. Steps
    come faster when something is overdue. Being on task resets it; unsure keeps it where it is."""

    def __init__(self, config: dict):
        self.config = config
        self.level = 0
        self.started: float | None = None     # when the drifting began

    def feed(self, verdict: str, moment: float, overdue: bool) -> int:
        if verdict == "on_task":
            self.level, self.started = 0, None
        elif verdict == "digressing":
            if self.started is None:
                self.started = moment
            first, second = self.config["steps_minutes_overdue" if overdue else "steps_minutes"]
            elapsed = (moment - self.started) / 60
            self.level = 1 + (elapsed >= first) + (elapsed >= first + second)
        return self.level

    def reset(self) -> None:
        self.level, self.started = 0, None


def active_window() -> tuple[str, str]:
    """(app, title) of the window in front, through Wnck. Empty strings if there is none."""
    import gi
    gi.require_version("Wnck", "3.0")
    from gi.repository import Wnck
    screen = Wnck.Screen.get_default()
    screen.force_update()
    window = screen.get_active_window()
    if window is None:
        return "", ""
    app = window.get_class_group_name() or (window.get_application().get_name() if window.get_application() else "")
    return app or "", window.get_name() or ""


def idle_seconds() -> float:
    """Seconds since the last key or mouse input, from GNOME; 0 if unknown."""
    from gi.repository import Gio, GLib
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        locked = bus.call_sync("org.gnome.ScreenSaver", "/org/gnome/ScreenSaver", "org.gnome.ScreenSaver",
                               "GetActive", None, None, Gio.DBusCallFlags.NONE, 2000, None).unpack()[0]
        if locked:
            return float("inf")
        idle = bus.call_sync("org.gnome.Mutter.IdleMonitor", "/org/gnome/Mutter/IdleMonitor/Core",
                             "org.gnome.Mutter.IdleMonitor", "GetIdletime", None, None,
                             Gio.DBusCallFlags.NONE, 2000, None).unpack()[0]
        return idle / 1000
    except GLib.Error:
        return 0.0


def log_path() -> Path:
    return data_dir() / "focus-log.jsonl"


def write_log(entry: dict, keep_days: int) -> None:
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a") as out:
        out.write(json.dumps(entry) + "\n")
    if datetime.now().minute == 0:            # trim now and then
        cutoff = (datetime.now() - timedelta(days=keep_days)).isoformat()
        lines = [l for l in path.read_text().splitlines() if l[10:36] >= cutoff[:26]]
        path.write_text("\n".join(lines) + "\n")


def summary(day: datetime | None = None) -> str:
    """Today's focus in words, from the log."""
    day_text = (day or datetime.now()).date().isoformat()
    try:
        entries = [json.loads(l) for l in log_path().read_text().splitlines() if l.strip()]
    except FileNotFoundError:
        entries = []
    entries = [e for e in entries if e.get("ts", "").startswith(day_text)]
    if not entries:
        return "No focus checks logged today."
    minutes = {}
    judges = {}
    drifts = {}
    for entry in entries:
        minutes[entry["verdict"]] = minutes.get(entry["verdict"], 0) + entry.get("minutes", 0)
        judges[entry.get("judge", "?")] = judges.get(entry.get("judge", "?"), 0) + 1
        if entry["verdict"] == "digressing":
            drifts[entry.get("site", "?")] = drifts.get(entry.get("site", "?"), 0) + entry.get("minutes", 0)
    lines = [f"Today: about {round(minutes.get('on_task', 0))} min on task, "
             f"{round(minutes.get('digressing', 0))} min drifting, {round(minutes.get('unsure', 0))} min unclear."]
    if drifts:
        lines.append("Drifting on: " + ", ".join(f"{k} {round(v)} min" for k, v in
                                                 sorted(drifts.items(), key=lambda kv: -kv[1])))
    lines.append("Decided by: " + ", ".join(f"{k} {v}×" for k, v in judges.items()))
    return "\n".join(lines)


class Watcher:
    """Samples the active window on a GLib timer and drives the pet. Model calls run in a thread."""

    def __init__(self, pet, read_window: Callable = active_window, read_idle: Callable = idle_seconds):
        self.pet = pet
        self.read_window, self.read_idle = read_window, read_idle
        self.config = load_config()
        self.judge_chain = Judge(self.config)
        self.escalation = Escalation(self.config)
        self.tasks: list[tuple[str, str]] = []
        self.overdue = False
        self.last: tuple[str, str] | None = None
        self.same = 0
        self.busy = False
        self.snoozed_until = 0.0
        self.current: tuple[str, str] = ("", "")
        self.shown = 0                        # judgy level the pet is showing
        from gi.repository import GLib
        self.GLib = GLib
        GLib.timeout_add_seconds(int(self.config["interval_seconds"]), self.sample)

    # ---- settings from the pet's menu ------------------------------------------------------------
    def enabled(self) -> bool:
        return bool(self.config["enabled"])

    def set_enabled(self, on: bool) -> None:
        self.config["enabled"] = on
        save_config({"enabled": on})
        if not on:
            self._calm()

    def snooze(self, minutes: int | None) -> None:
        if minutes is None:
            tomorrow = (datetime.now() + timedelta(days=1)).replace(hour=6, minute=0, second=0)
            self.snoozed_until = tomorrow.timestamp()
        else:
            self.snoozed_until = time.time() + minutes * 60
        self._calm()

    def set_tasks(self, tasks: list[tuple[str, str]], overdue: bool) -> None:
        self.tasks, self.overdue = tasks, overdue
        if not tasks and self.shown:
            self._calm()                           # nothing urgent: you're free

    # ---- sampling --------------------------------------------------------------------------------
    def sample(self) -> bool:
        if not self.enabled() or time.time() < self.snoozed_until or not self.tasks or self.busy:
            return True
        if self.read_idle() > self.config["idle_minutes"] * 60:
            self.same = 0
            return True
        app, title = self.read_window()
        if not title or "nudgy" in title.lower() or app.lower() in ("nudgy", "__main__.py"):
            return True
        self.same = self.same + 1 if (app, title) == self.last else 1
        self.last = (app, title)
        if self.same < self.config["same_window_samples"]:
            return True
        self.busy = True
        tasks = list(self.tasks)
        threading.Thread(target=self._judge_in_background, args=(app, title, tasks), daemon=True).start()
        return True

    def _judge_in_background(self, app: str, title: str, tasks) -> None:
        try:
            verdict = self.judge_chain.decide(app, title, tasks)
        except Exception as error:      # a judge failing must never stop the pet
            log.warning("focus judge failed: %s: %s", type(error).__name__, error)
            verdict = Verdict("unsure", 0.0, "", f"error: {error}", "none")
        self.GLib.idle_add(self._react, app, title, tasks, verdict)

    def _react(self, app: str, title: str, tasks, verdict: Verdict) -> bool:
        self.busy = False
        self.current = (app, title)
        site = site_of(app, title, self.config)
        write_log({"ts": datetime.now().isoformat(timespec="seconds"), "app": app, "title": title[:200],
                   "site": site, "verdict": verdict.verdict, "confidence": verdict.confidence,
                   "judge": verdict.judge, "reason": verdict.reason,
                   "minutes": self.config["interval_seconds"] / 60}, self.config["log_days"])
        level = self.escalation.feed(verdict.verdict, time.time(), self.overdue)
        if level == self.shown:
            return False
        if level == 0:
            self.shown = 0
            self.pet.forgive()
            return False
        self.shown = level
        task = verdict.task or (tasks[0][0] if tasks else "your urgent work")
        task = task if len(task) <= 60 else task[:59] + "…"
        when = next((w for t, w in tasks if t == verdict.task), tasks[0][1] if tasks else "")
        if level == 1:
            text = f"Hmm. Is {site} helping with “{task}”?"
            buttons = (("Yes, it is", lambda: self._trust(app, title, tasks)), ("Back to work", self._back))
        elif level == 2:
            text = f"Still {site}… “{task}” is due {when}." if when else f"Still {site}… “{task}” is waiting."
            buttons = (("Okay, okay", self._back), ("It's on task", lambda: self._trust(app, title, tasks)))
        else:
            text = f"I'm not watching this. Come get me when you're back on “{task}”."
            buttons = (("I'm back", self._back),)
        self.pet.judge(level, text, buttons)
        return False

    def _calm(self) -> None:
        self.escalation.reset()
        self.shown = 0
        self.pet.forgive(happy=False)

    def _trust(self, app: str, title: str, tasks) -> None:
        self.judge_chain.allow(app, title, tasks)
        self._calm()

    def _back(self) -> None:
        """You promised to go back. The drifting clock keeps running until a check sees you on task."""
        self.shown = 0
        self.pet.forgive()
