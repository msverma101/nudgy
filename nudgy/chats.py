"""Signal and WhatsApp, read-only. Neither has an official API for personal accounts.

Signal: signal-cli is linked to your phone as an extra device, like Signal Desktop. Each sync asks it
for new messages. Messages leave the list when you read them on another device or reply.

WhatsApp: a small Node bridge (bridges/whatsapp) stays connected like WhatsApp Web and keeps
whatsapp-chats.json up to date. This is unofficial and against WhatsApp's terms; it only reads.

Unlinked services are skipped quietly: they are not errors.
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from .model import Item, data_dir

KEEP_DAYS = 14
BRIDGE = Path(__file__).resolve().parent.parent / "bridges" / "whatsapp"
SIGNAL_TIMEOUT = 90


class NotReady(Exception):
    """A service is linked but can't be read right now; the message says why."""


# ---- shared ------------------------------------------------------------------------------------
def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return default


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix(".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)   # holds message text
    with os.fdopen(fd, "w") as out:
        json.dump(value, out)
    os.replace(temp, path)


def _clip(text: str, limit: int = 140) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def node_path() -> str | None:
    """The systemd service has no nvm in PATH, so look in nvm's folder too."""
    found = shutil.which("node")
    if found:
        return found
    stable = [p for p in glob.glob(str(Path.home() / ".nvm/versions/node/*/bin/node"))
              if "-" not in Path(p).parts[-3]]          # skip alpha/nightly builds
    stable.sort(key=lambda p: [int(x) for x in Path(p).parts[-3].lstrip("v").split(".")])
    return stable[-1] if stable else None


def bridge_installed() -> bool:
    return (BRIDGE / "node_modules" / "baileys").is_dir()


def bridge_command(*args: str) -> list[str]:
    node = node_path()
    if not node:
        raise NotReady("Node.js not found")
    if not bridge_installed():
        raise NotReady(f"WhatsApp bridge not installed. Run: cd {BRIDGE} && npm install")
    return [node, str(BRIDGE / "wa.mjs"), *args]


def bridge_env() -> dict:
    return {**os.environ, "NUDGY_DATA": str(data_dir())}


def bridge_log():
    path = data_dir() / "whatsapp-bridge.log"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists() and path.stat().st_size > 1_000_000:
        path.unlink()
    return open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a")


def make_qr(text: str, png: Path) -> None:
    subprocess.run(bridge_command("qr", text, str(png)), env=bridge_env(), check=True, timeout=30,
                   capture_output=True)


# ---- Signal ------------------------------------------------------------------------------------
def signal_cli() -> str | None:
    return shutil.which("signal-cli") or next(
        (p for p in [str(Path.home() / ".local/bin/signal-cli")] if os.access(p, os.X_OK)), None)


def signal_account() -> str | None:
    """The linked phone number, read from signal-cli's own account list."""
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "signal-cli/data"
    accounts = _read_json(base / "accounts.json", {}).get("accounts", [])
    return accounts[0].get("number") if accounts else None


def link_signal(on_uri: Callable[[str], None], on_start: Callable | None = None) -> str:
    """Runs `signal-cli link`. Calls on_uri with the sgnl:// link to show as a QR code, then waits
    for the phone to scan it. Returns the linked number."""
    cli = signal_cli()
    if not cli:
        raise NotReady("signal-cli is not installed")
    process = subprocess.Popen([cli, "link", "-n", "Nudgy"], stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True)
    if on_start:
        on_start(process)          # lets the window cancel linking
    output = []
    for line in process.stdout:
        output.append(line.strip())
        if line.startswith("sgnl://"):
            on_uri(line.strip())
    if process.wait() != 0:
        raise NotReady(_clip(" ".join(output[-2:]) or "Linking failed", 160))
    return signal_account() or "linked"


def _signal_who(env: dict) -> str:
    return env.get("sourceUuid") or env.get("sourceNumber") or env.get("source") or ""


def apply_signal(inbox: dict, lines: list[dict], now: datetime) -> dict:
    """Updates the pending-message inbox from `signal-cli -o json receive` output.
    inbox: conversation id -> {"name", "group", "messages": [{"ts", "text", "from", "sender"}]}"""
    for line in lines:
        env = line.get("envelope", line)
        data, sync = env.get("dataMessage"), env.get("syncMessage") or {}
        if data and (data.get("message") or data.get("attachments")):
            group = data.get("groupInfo") or {}
            who = _signal_who(env)
            conv = f"group:{group['groupId']}" if group.get("groupId") else who
            if not conv:
                continue
            entry = inbox.setdefault(conv, {"name": "", "group": bool(group), "messages": []})
            entry["name"] = group.get("groupName") or entry["name"] or (
                "" if group else env.get("sourceName") or env.get("sourceNumber") or "")
            text = data.get("message") or "[attachment]"
            entry["messages"].append({"ts": data.get("timestamp") or env.get("timestamp", 0), "text": text,
                                      "from": env.get("sourceName") or env.get("sourceNumber") or "",
                                      "sender": [v for v in (env.get("sourceUuid"), env.get("sourceNumber")) if v]})
        sent = sync.get("sentMessage")
        if sent:                                  # you replied from another device
            group = sent.get("groupInfo") or {}
            conv = f"group:{group['groupId']}" if group.get("groupId") else (
                sent.get("destinationUuid") or sent.get("destinationNumber") or sent.get("destination"))
            if conv:
                inbox.pop(conv, None)
                inbox.pop(sent.get("destinationNumber") or "", None)
        for read in sync.get("readMessages") or []:   # you read it on the phone or Signal Desktop
            senders = {v for v in (read.get("senderUuid"), read.get("senderNumber"), read.get("sender")) if v}
            for entry in inbox.values():
                entry["messages"] = [m for m in entry["messages"]
                                     if not (senders & set(m["sender"]) and m["ts"] <= read.get("timestamp", 0))]
    cutoff = (now - timedelta(days=KEEP_DAYS)).timestamp() * 1000
    for conv in list(inbox):
        inbox[conv]["messages"] = [m for m in inbox[conv]["messages"] if m["ts"] >= cutoff]
        if not inbox[conv]["messages"]:
            del inbox[conv]
    return inbox


def signal_items(inbox: dict) -> list[Item]:
    items = []
    for conv, entry in inbox.items():
        messages = sorted(entry["messages"], key=lambda m: m["ts"])
        last = messages[-1]
        name = entry["name"] or last["from"] or "Signal"
        text = f"{last['from']}: {last['text']}" if entry["group"] and last["from"] else last["text"]
        more = f" · {len(messages)} new" if len(messages) > 1 else ""
        items.append(Item(id=f"signal:{conv}", source="signal", title=_clip(text), detail=name + more,
                          due=None, tags=["group" if entry["group"] else "direct"]))
    return items


def fetch_signal(now: datetime) -> list[Item] | None:
    cli, account = signal_cli(), signal_account()
    if not cli or not account:
        return None
    path = data_dir() / "signal-inbox.json"
    inbox = _read_json(path, {})
    result = subprocess.run(
        [cli, "-o", "json", "-a", account, "receive", "-t", "3", "--ignore-attachments",
         "--ignore-stories", "--ignore-avatars", "--ignore-stickers"],
        capture_output=True, text=True, timeout=SIGNAL_TIMEOUT)
    lines = []
    for raw in result.stdout.splitlines():
        try:
            lines.append(json.loads(raw))
        except ValueError:
            continue
    if result.returncode != 0 and not lines:
        tail = (result.stderr.strip().splitlines() or ["signal-cli failed"])[-1]
        raise NotReady(_clip(tail, 100))
    _write_json(path, apply_signal(inbox, lines, now))   # receive consumes messages: save them first
    return signal_items(inbox)


# ---- WhatsApp ----------------------------------------------------------------------------------
def whatsapp_linked() -> bool:
    creds = _read_json(data_dir() / "whatsapp-auth" / "creds.json", {})
    return bool(creds.get("me"))


def whatsapp_items(db: dict, now: datetime) -> list[Item]:
    items = []
    names = db.get("names", {})
    for jid, chat in db.get("chats", {}).items():
        if chat.get("unread", 0) <= 0 or chat.get("archived"):
            continue
        if chat.get("muted") and (chat["muted"] < 0 or chat["muted"] > now.timestamp()):
            continue                                   # muted chats stay quiet here too
        fallback = "+" + jid.split("@")[0] if jid.endswith("@s.whatsapp.net") else (
            "WhatsApp group" if chat.get("group") else "WhatsApp")
        name = names.get(jid) or fallback
        last = chat.get("last") or "New message"
        text = f"{chat['from']}: {last}" if chat.get("group") and chat.get("from") else last
        more = f" · {chat['unread']} new" if chat["unread"] > 1 else ""
        number = jid.split("@")[0] if jid.endswith("@s.whatsapp.net") else ""
        url = f"https://web.whatsapp.com/send?phone={number}" if number.isdigit() else "https://web.whatsapp.com/"
        items.append(Item(id=f"whatsapp:{jid}", source="whatsapp", title=_clip(text), detail=name + more, url=url,
                          tags=["group" if chat.get("group") else "direct"]))
    return items


def fetch_whatsapp(now: datetime) -> list[Item] | None:
    if not whatsapp_linked():
        return None
    db = _read_json(data_dir() / "whatsapp-chats.json", None)
    if db is None or time.time() - db.get("updated", 0) > 5 * 60:
        raise NotReady("bridge not running (systemctl --user status nudgy-whatsapp)")
    return whatsapp_items(db, now)


def whatsapp_service(action: str) -> None:
    subprocess.run(["systemctl", "--user", action, *(["--now"] if action in ("enable", "disable") else []),
                    "nudgy-whatsapp.service"], check=False, capture_output=True)


def link_whatsapp(on_qr: Callable[[str], None], on_start: Callable | None = None) -> None:
    """Shows each QR code WhatsApp issues (they rotate about every 20 s) until the phone scans one."""
    whatsapp_service("stop")
    log = bridge_log()
    process = subprocess.Popen(bridge_command("link"), stdout=subprocess.PIPE, stderr=log,
                               text=True, env=bridge_env())
    log.close()
    if on_start:
        on_start(process)
    linked = False
    for raw in process.stdout:
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if "qr" in event:
            on_qr(event["qr"])
        linked = linked or bool(event.get("linked"))
    process.wait()
    if not linked:
        raise NotReady(f"WhatsApp linking did not finish. Details: {data_dir() / 'whatsapp-bridge.log'}")
    whatsapp_service("enable")


def unlink_whatsapp() -> None:
    whatsapp_service("disable")
    subprocess.run(bridge_command("logout"), env=bridge_env(), timeout=60, capture_output=True)
