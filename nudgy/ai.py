"""Language models for Nudgy: a local model through Ollama by default; Claude only if you connect it.

Every model call goes through complete(job, system, user). The jobs are:
  talk   understanding what you said to the pet (always local: it must be fast and private)
  draft  writing a reply draft for you to edit
  steps  breaking a note into steps
  focus  judging a window title when the local model is unsure (the focus check)
  rank   re-ranking the list when the local model's answer doesn't pass the check

Connecting Claude is optional, in two ways (settings "ai.claude"):
  "api"          your own Anthropic API key, through Anthropic's Python library (installed on connect)
  "claude-code"  your Claude Code login, through the `claude` command (no tools, nothing saved)
Then "ai.claude_for" says which jobs may use it. Not connected: nothing is ever sent to Anthropic.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import models
from . import settings as prefs
from .model import config_dir, data_dir, quiet_claude_env

log = logging.getLogger(__name__)

JOBS = ("talk", "draft", "steps", "focus", "rank")


class Unavailable(Exception):
    """No model can do this job right now (Ollama not running, Claude not connected, ...)."""


@dataclass
class Answer:
    text: str
    tokens: int = 0
    by: str = "local"           # "local" or "claude"


# ---- the local model (Ollama) -----------------------------------------------------------------------------
def local(system: str, user: str, json_mode: bool = False, timeout: float = 180, history=None,
          model: str | None = None) -> Answer:
    """Asks the local model, with reasoning switched off and stripped if a model prints it anyway."""
    config = prefs.section("ai")
    body: dict = {"model": model or models.current(), "stream": False, "think": False, "keep_alive": "10m",
                  "options": {"temperature": 0.3, "num_ctx": 4096},
                  "messages": [{"role": "system", "content": system}, *(history or []),
                               {"role": "user", "content": user}]}
    if json_mode:
        body["format"] = "json"
    request = urllib.request.Request(config["ollama_url"].rstrip("/") + "/api/chat", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content = json.load(response)["message"]["content"]
    except (OSError, ValueError, KeyError) as error:
        raise Unavailable(f"the local model didn't answer ({error}). Is Ollama running?") from error
    return Answer(clean(content), 0, "local")


def clean(text: str) -> str:
    """Some models print their reasoning first (sometimes without the opening tag): keep what follows."""
    return text.split("</think>")[-1].strip()


# ---- Claude (optional) ------------------------------------------------------------------------------------
def key_path() -> Path:
    return config_dir() / "anthropic.json"


def api_key() -> str | None:
    try:
        return json.loads(key_path().read_text()).get("api_key") or None
    except (FileNotFoundError, ValueError):
        return None


def save_key(key: str) -> None:
    key = key.strip()
    if not key.startswith("sk-ant-"):
        raise ValueError("That doesn't look like an Anthropic API key (they start with sk-ant-).")
    key_path().parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = os.open(key_path(), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "w") as out:
        json.dump({"api_key": key}, out)


def forget_key() -> None:
    key_path().unlink(missing_ok=True)


def claude_code() -> str | None:
    return shutil.which("claude") or next((p for p in [str(Path.home() / ".local/bin/claude")]
                                           if os.access(p, os.X_OK)), None)


def claude_connected() -> str:
    """"api", "claude-code", or "" when Claude isn't connected (or its credentials are gone)."""
    way = prefs.section("ai")["claude"]
    if way == "api" and api_key():
        return "api"
    if way == "claude-code" and claude_code():
        return "claude-code"
    return ""


def uses_claude(job: str) -> bool:
    return job != "talk" and bool(claude_connected()) and job in prefs.section("ai")["claude_for"]


def claude(system: str, user: str, timeout: float = 120) -> Answer:
    config = prefs.section("ai")
    way = claude_connected()
    if way == "api":
        return _claude_api(system, user, config["claude_model"], timeout)
    if way == "claude-code":
        return _claude_code(system, user, config["claude_model"], timeout)
    raise Unavailable("Claude isn't connected")


def _claude_api(system: str, user: str, model: str, timeout: float) -> Answer:
    try:
        import anthropic                                   # optional: installed by `nudgy claude connect`
    except ImportError as error:
        raise Unavailable("Anthropic's library isn't installed. Run: nudgy claude connect") from error
    client = anthropic.Anthropic(api_key=api_key(), timeout=timeout, max_retries=2)
    try:
        response = client.messages.create(model=model, max_tokens=4096, system=system,
                                          messages=[{"role": "user", "content": user}])
    except anthropic.AuthenticationError as error:
        raise Unavailable("Anthropic didn't accept the API key; connect again: nudgy claude connect") from error
    except anthropic.RateLimitError as error:
        raise Unavailable("Anthropic's rate limit was reached; try again in a minute") from error
    except anthropic.APIStatusError as error:
        raise Unavailable(f"Anthropic returned an error ({error.status_code})") from error
    except anthropic.APIConnectionError as error:
        raise Unavailable("couldn't reach Anthropic (offline?)") from error
    if response.stop_reason == "refusal":
        raise Unavailable("Claude declined this one")
    text = "".join(block.text for block in response.content if block.type == "text")
    usage = response.usage
    return Answer(text.strip(), (usage.input_tokens or 0) + (usage.output_tokens or 0), "claude")


def _claude_code(system: str, user: str, model: str, timeout: float) -> Answer:
    command = [claude_code() or "claude", "-p", user, "--model", model, "--output-format", "json", "--tools", "",
               "--strict-mcp-config", "--no-session-persistence", "--setting-sources", "",
               "--system-prompt", system]
    folder = data_dir()
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, cwd=str(folder),
                                env=quiet_claude_env())
        answer = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError) as error:
        raise Unavailable(f"Claude Code didn't answer ({error})") from error
    if answer.get("is_error"):
        raise Unavailable(str(answer.get("result", "Claude Code failed"))[:120])
    usage = answer.get("usage", {})
    tokens = sum(usage.get(k, 0) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                                           "cache_read_input_tokens"))
    return Answer(str(answer.get("result", "")).strip(), tokens, "claude")


# ---- one entry point ---------------------------------------------------------------------------------------
def complete(job: str, system: str, user: str, json_mode: bool = False, timeout: float = 180) -> Answer:
    """The answer for one job, from Claude if you connected it for that job, otherwise the local model."""
    if job not in JOBS:
        raise ValueError(f"unknown job {job!r}")
    if uses_claude(job):
        try:
            return claude(system, user + ("\n\nAnswer with JSON only." if json_mode else ""), timeout)
        except Unavailable as error:
            log.warning("Claude unavailable for %s (%s); using the local model", job, error)
    return local(system, user, json_mode, timeout)


def install_library() -> None:
    """Installs Anthropic's Python library into the environment Nudgy runs in."""
    if "pipx" in sys.prefix and shutil.which("pipx"):          # pipx environments have no pip of their own
        command = ["pipx", "inject", "nudgy", "anthropic>=1,<2"]
    else:
        command = [sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check", "anthropic>=1,<2"]
    subprocess.run(command, check=True)
