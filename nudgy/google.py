"""Read-only Google access using only the standard library.

Google Keep has no API for personal accounts, but Keep reminders appear in Google Tasks.
LinkedIn has no personal inbox API, so its notification emails are read from Gmail.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta
from email.utils import parseaddr
from pathlib import Path

from .model import Item, config_dir

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly",
          "https://www.googleapis.com/auth/tasks.readonly",
          "https://www.googleapis.com/auth/gmail.readonly"]
# Asked for only when you want Nudgy to add things to Google (by voice), in a separate token, so your
# read-only sign-in is never widened. events: create/delete events you make; tasks: create/delete tasks.
WRITE_SCOPES = ["https://www.googleapis.com/auth/calendar.events",
                "https://www.googleapis.com/auth/tasks"]
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
def skip_calendars() -> set[str]:
    """Calendar names to leave out, from ~/.config/nudgy/google.json: {"skip_calendars": ["Alarms"]}."""
    try:
        return set(json.loads((config_dir() / "google.json").read_text()).get("skip_calendars", []))
    except (FileNotFoundError, ValueError, AttributeError):
        return set()
LINKEDIN_KINDS = {"messages-noreply": "message", "invitations": "invitation",
                  "jobs-noreply": "job", "jobalerts-noreply": "job"}
MAX_MESSAGES = 15
# Google's built-in public holiday and birthday calendars.
SKIP_CALENDAR_KINDS = ("#holiday@group.v.calendar.google.com", "#contacts@group.v.calendar.google.com")


class NeedsSignIn(Exception):
    """The user has to run `nudgy auth`."""


def client_path() -> Path:
    return config_dir() / "google-client.json"


def token_path() -> Path:
    return config_dir() / "token.json"


def _client() -> dict:
    try:
        raw = json.loads(client_path().read_text())
    except FileNotFoundError:
        raise NeedsSignIn("Google not set up. Click 👤") from None
    return raw.get("installed") or raw["web"]


def import_client(source: Path) -> None:
    """Copies the client JSON downloaded from Google Cloud into Nudgy's config folder."""
    raw = json.loads(Path(source).read_text())
    client = raw.get("installed") or raw.get("web") or {}
    if not client.get("client_id"):
        raise ValueError("That file is not a Google OAuth client file (no client_id).")
    path = config_dir() / "google-client.json"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as out:
        json.dump(raw, out)


def sign_out() -> None:
    token_path().unlink(missing_ok=True)


def _write_token(token: dict, path: Path | None = None) -> None:
    path = path or token_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as out:
        json.dump(token, out)


def _post_token(fields: dict) -> dict:
    request = urllib.request.Request(TOKEN_URL, urllib.parse.urlencode(fields).encode())
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def authorize(timeout: int = 300, scopes: list[str] | None = None, path: Path | None = None) -> None:
    """Browser sign-in using a loopback redirect with PKCE. Keep uses its own scopes and token file,
    so a refusal there can't break Calendar, Tasks or Gmail."""
    client = _client()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    result: dict = {}

    class Callback(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if query.get("state") == [state]:
                result.update({k: v[0] for k, v in query.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Nudgy: you can close this tab.")

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Callback)
    server.timeout = 5
    redirect = f"http://127.0.0.1:{server.server_port}/"
    webbrowser.open(AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client["client_id"], "redirect_uri": redirect, "response_type": "code",
        "scope": " ".join(scopes or SCOPES), "state": state, "access_type": "offline", "prompt": "consent",
        "code_challenge": challenge, "code_challenge_method": "S256"}))
    deadline = time.monotonic() + timeout
    while not result and time.monotonic() < deadline:
        server.handle_request()
    server.server_close()
    if "code" not in result:
        raise NeedsSignIn(f"Sign-in did not finish: {result.get('error', 'timed out')}")
    token = _post_token({
        "client_id": client["client_id"], "client_secret": client.get("client_secret", ""),
        "code": result["code"], "code_verifier": verifier, "grant_type": "authorization_code",
        "redirect_uri": redirect})
    token["expires_at"] = time.time() + token.get("expires_in", 3600)
    _write_token(token, path)


def access_token(path: Path | None = None) -> str:
    try:
        token = json.loads((path or token_path()).read_text())
    except FileNotFoundError:
        raise NeedsSignIn("Not signed in. Click 👤 to sign in") from None
    if time.time() < token.get("expires_at", 0) - 60:
        return token["access_token"]
    client = _client()
    try:
        fresh = _post_token({
            "client_id": client["client_id"], "client_secret": client.get("client_secret", ""),
            "refresh_token": token["refresh_token"], "grant_type": "refresh_token"})
    except urllib.error.HTTPError as error:
        if error.code in (400, 401):
            raise NeedsSignIn("Sign-in expired. Click 👤 to sign in") from None
        raise
    token.update(fresh)
    token["expires_at"] = time.time() + fresh.get("expires_in", 3600)
    _write_token(token, path)
    return token["access_token"]


def api(url: str, params: dict | None = None) -> dict:
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token()}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def write_token_path() -> Path:
    return config_dir() / "token-write.json"


def can_write() -> bool:
    return write_token_path().exists()


def authorize_write() -> None:
    authorize(scopes=WRITE_SCOPES, path=write_token_path())


def send(method: str, url: str, body: dict | None = None) -> dict:
    """A change on Google with the write token: POST to create, DELETE to remove."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {access_token(write_token_path())}", "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read()
    return json.loads(raw) if raw else {}


TASKS = "https://tasks.googleapis.com/tasks/v1/lists/@default/tasks"
EVENTS = "https://www.googleapis.com/calendar/v3/calendars/primary/events"


def add_task(title: str, due: datetime | None = None) -> str:
    """Adds to your default Google Tasks list. Returns an id for undo. Tasks keeps only the date."""
    body = {"title": title}
    if due:
        body["due"] = due.date().isoformat() + "T00:00:00.000Z"
    return send("POST", TASKS, body)["id"]


def add_event(title: str, start: datetime, minutes: int = 60, all_day: bool = False) -> str:
    """Adds to your main Google Calendar. Returns an id for undo."""
    if all_day:
        body = {"summary": title, "start": {"date": start.date().isoformat()},
                "end": {"date": (start.date() + timedelta(days=1)).isoformat()}}
    else:
        body = {"summary": title, "start": {"dateTime": start.isoformat()},
                "end": {"dateTime": (start + timedelta(minutes=minutes)).isoformat()}}
    return send("POST", EVENTS, body)["id"]


def remove(kind: str, google_id: str) -> None:
    """Undo for add_task / add_event."""
    url = f"{TASKS}/{google_id}" if kind == "task" else f"{EVENTS}/{google_id}"
    send("DELETE", url)


def message_text(item_id: str, limit: int = 4000) -> tuple[str, str, str]:
    """(sender, subject, plain text) of a Gmail item, read-only, for drafting a reply."""
    message_id = item_id.split(":", 1)[1]
    message = api(f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}", {"format": "full"})
    headers = _headers(message)

    def plain(part: dict) -> str:
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(part["body"]["data"] + "===").decode("utf-8", "replace")
        return "\n".join(filter(None, (plain(p) for p in part.get("parts", []) or [])))

    text = plain(message.get("payload", {})) or message.get("snippet", "")
    return headers.get("from", ""), headers.get("subject", ""), re.sub(r"\n{3,}", "\n\n", text)[:limit]


def fetch_calendar(now: datetime, days: int = 7) -> list[Item]:
    base = "https://www.googleapis.com/calendar/v3"
    calendars = api(f"{base}/users/me/calendarList").get("items", [])
    skipped = skip_calendars()
    items = []
    for calendar in calendars:
        if not calendar.get("selected") or calendar.get("summary") in skipped:
            continue
        if calendar["id"].endswith(SKIP_CALENDAR_KINDS):
            continue        # public holidays and contacts' birthdays aren't things to do
        events = api(f"{base}/calendars/{urllib.parse.quote(calendar['id'], safe='')}/events", {
            "singleEvents": "true", "orderBy": "startTime", "maxResults": 50,
            "timeMin": now.isoformat(), "timeMax": (now + timedelta(days=days)).isoformat()})
        for event in events.get("items", []):
            if event.get("status") == "cancelled":
                continue
            start, end = event.get("start", {}), event.get("end", {})
            items.append(Item(
                id=f"calendar:{calendar['id']}:{event['id']}", source="calendar",
                title=event.get("summary") or "(no title)", detail=event.get("location", ""),
                due=start.get("dateTime") or start.get("date"), end=end.get("dateTime") or end.get("date"),
                url=event.get("htmlLink", "")))
    return items


def fetch_tasks() -> list[Item]:
    base = "https://tasks.googleapis.com/tasks/v1"
    items = []
    for task_list in api(f"{base}/users/@me/lists").get("items", []):
        if task_list.get("title", "").startswith("Old Google Keep Reminders"):
            continue
        tasks = api(f"{base}/lists/{task_list['id']}/tasks",
                    {"showCompleted": "false", "showHidden": "false", "maxResults": 100})
        for task in tasks.get("items", []):
            if not task.get("title", "").strip():
                continue
            # Tasks stores only a date; the time part of `due` is always midnight UTC.
            due = task["due"][:10] if task.get("due") else None
            items.append(Item(id=f"task:{task['id']}", source="task", title=task["title"].strip(),
                              detail=task_list.get("title", ""), due=due, url=task.get("webViewLink", "")))
    return items


def _headers(message: dict) -> dict[str, str]:
    return {h["name"].lower(): h["value"] for h in message.get("payload", {}).get("headers", [])}


def _messages(query: str) -> list[dict]:
    base = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
    found = api(base, {"q": query, "maxResults": MAX_MESSAGES}).get("messages", [])
    return [api(f"{base}/{m['id']}", {"format": "metadata", "metadataHeaders": ["Subject", "From"]})
            for m in found]


def _mail_url(account: str, message_id: str) -> str:
    return f"https://mail.google.com/mail/u/{urllib.parse.quote(account, safe='@')}/#all/{message_id}"


def _account() -> str:
    return api("https://gmail.googleapis.com/gmail/v1/users/me/profile")["emailAddress"]


def fetch_gmail() -> list[Item]:
    account = _account()
    items = []
    query = "is:unread in:inbox (is:important OR is:starred) newer_than:14d -category:promotions -from:linkedin.com"
    for message in _messages(query):
        headers, labels = _headers(message), message.get("labelIds", [])
        name, address = parseaddr(headers.get("from", ""))
        tags = [tag for tag, label in (("starred", "STARRED"), ("important", "IMPORTANT")) if label in labels]
        items.append(Item(id=f"gmail:{message['id']}", source="gmail",
                          title=headers.get("subject") or "(no subject)", detail=name or address,
                          url=_mail_url(account, message["id"]), tags=tags))
    return items


def luma_applications() -> list[str]:
    """Names of events you registered, applied or are waitlisted for, from Luma's emails (subjects only).
    Luma's calendar link leaves out registrations still waiting for approval."""
    query = ('from:(luma-mail.com OR lu.ma OR luma.com) newer_than:120d subject:("pending approval" OR '
             '"waiting list" OR "Registration approved" OR "Registration confirmed")')
    names = []
    for message in _messages(query):
        found = re.search(r"(?:approval|list|approved|confirmed) for (.+)$", _headers(message).get("subject", ""))
        if found:
            names.append(found.group(1).strip())
    return names


def fetch_linkedin() -> list[Item]:
    account = _account()
    items = []
    for message in _messages("is:unread from:linkedin.com newer_than:14d"):
        headers = _headers(message)
        _, address = parseaddr(headers.get("from", ""))
        kind = LINKEDIN_KINDS.get(address.split("@")[0].lower())
        if kind is None:
            continue    # digests, "who viewed your profile" and similar noise
        items.append(Item(id=f"linkedin:{message['id']}", source="linkedin",
                          title=headers.get("subject") or "(no subject)", detail=kind.capitalize(),
                          url=_mail_url(account, message["id"]), tags=[kind]))
    return items


def describe(error: Exception) -> str:
    if isinstance(error, NeedsSignIn):
        return str(error)
    if isinstance(error, urllib.error.HTTPError):
        return f"Google returned {error.code}"
    if isinstance(error, (urllib.error.URLError, TimeoutError, OSError)):
        return "Offline"
    return re.sub(r"\s+", " ", f"{type(error).__name__}: {error}")[:120]


def sync_all(now: datetime) -> tuple[dict[str, list[Item]], dict[str, str]]:
    """Returns items per source and errors per source. Failed sources are left out of the items."""
    jobs = {"calendar": lambda: fetch_calendar(now), "task": fetch_tasks,
            "gmail": fetch_gmail, "linkedin": fetch_linkedin}
    items, errors = {}, {}
    for name, job in jobs.items():
        try:
            result = job()
            if result is not None:          # None: switched off
                items[name] = result
        except Exception as error:      # one broken source must not stop the others
            errors[name] = describe(error)
    return items, errors
