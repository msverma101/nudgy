"""Runs every source once. Each source fails on its own without stopping the others."""
from __future__ import annotations

from datetime import datetime, timedelta

from . import chats, claude_notes, discover, google, keep, luma
from .model import Item


GROUPS = ("google", "signal", "whatsapp", "keep", "claude", "events")


def sync_all(now: datetime) -> tuple[dict[str, list[Item]], dict[str, str]]:
    return sync_groups(now, set(GROUPS))


def sync_groups(now: datetime, groups: set[str], calendar: list[Item] | None = None
                ) -> tuple[dict[str, list[Item]], dict[str, str]]:
    """Fetches only the named groups. `calendar` is the last good Calendar list, used to drop Luma
    events that are already in Google Calendar when Calendar itself isn't fetched this round."""
    items, errors = google.sync_all(now) if "google" in groups else ({}, {})
    for name, job in (("signal", chats.fetch_signal), ("whatsapp", chats.fetch_whatsapp)):
        if name not in groups:
            continue
        try:
            result = job(now)
        except Exception as error:
            errors[name] = str(error) if isinstance(error, chats.NotReady) else google.describe(error)
            continue
        if result is not None:              # None: not linked, so nothing to report
            items[name] = result
    if "keep" in groups:
        try:
            notes = keep.fetch_keep(now)
            if notes is not None:
                items["keep"] = notes
        except keep.KeepRefused as error:
            errors["keep"] = str(error)
        except Exception as error:
            errors["keep"] = google.describe(error)
    if "claude" in groups:
        try:
            items["claude"] = claude_notes.read(now)
        except Exception as error:
            errors["claude"] = google.describe(error)
    if "events" not in groups:
        return items, errors
    known_calendar = items.get("calendar", calendar or [])
    everything = None
    try:
        everything = luma.fetch_luma(now, days=discover.DAYS_AHEAD)
        horizon = now + timedelta(days=luma.DAYS_AHEAD)
        events = None if everything is None else [e for e in everything if e.due[:10] <= horizon.date().isoformat()]
        if events is not None:
            items["luma"] = luma.drop_duplicates(events, known_calendar)
    except Exception as error:
        errors["luma"] = str(error) if isinstance(error, ValueError) else google.describe(error)
    try:
        # Public events across Europe that fit you, minus the ones you are already going to.
        mine = (everything or []) + known_calendar
        found, applied = discover.fetch(now, applied=google.luma_applications)
        items["potential"] = discover.drop_known(found, mine, applied)
    except Exception as error:
        errors["potential"] = google.describe(error)
    return items, errors


def status() -> dict[str, str]:
    """One line per service for the Accounts window and `nudgy accounts`."""
    signed_in = google.token_path().exists()
    has_client = google.client_path().exists()
    return {
        "google": "Signed in" if signed_in else "Signed out" if has_client else "Not set up",
        "linkedin": "Through Gmail" if signed_in else "Needs Google",
        "luma": "Connected" if luma.feed_url() else "Not connected",
        "keep": keep.status(),
        "signal": f"Linked ({chats.signal_account()})" if chats.signal_account() else
                  "Not linked" if chats.signal_cli() else "signal-cli not installed",
        "whatsapp": "Linked" if chats.whatsapp_linked() else
                    "Not linked" if chats.bridge_installed() else "Bridge not installed",
    }
