"""Every source, as a plugin (see base.py). Runs them once; each fails on its own without stopping the
others."""
from __future__ import annotations

import importlib
import logging
from datetime import datetime

from ..model import Item
from .base import Round, Source

log = logging.getLogger(__name__)

PLUGINS = ("google", "signal", "keep", "claude", "events")      # files in this folder


def plugins() -> list[Source]:
    found = [importlib.import_module(f"{__name__}.{name}").SOURCE for name in PLUGINS]
    return sorted(found, key=lambda source: source.order)


GROUPS = tuple(source.group for source in plugins())


def sync_all(now: datetime) -> tuple[dict[str, list[Item]], dict[str, str]]:
    return sync_groups(now, set(GROUPS))


def sync_groups(now: datetime, groups: set[str], calendar: list[Item] | None = None
                ) -> tuple[dict[str, list[Item]], dict[str, str]]:
    """Fetches only the named groups. `calendar` is the last good Calendar list, used to drop Luma
    events that are already in Google Calendar when Calendar itself isn't fetched this round."""
    round = Round(now, calendar=calendar)
    for source in plugins():
        if source.group not in groups:
            continue
        try:
            round.items.update(source.fetch(round))
        except Exception as error:          # one broken source must not stop the others
            log.warning("%s failed: %s", source.group, source.explain(error))
            round.errors[source.group] = source.explain(error)
    return round.items, round.errors


def status() -> dict[str, str]:
    """One line per service for the Accounts window and `nudgy accounts`."""
    from .. import ai
    lines = {"anthropic": {"api": "Connected (your API key)", "claude-code": "Connected (Claude Code login)"}.get(
        ai.claude_connected(), "Not connected: everything stays on this computer")}
    for source in plugins():
        lines.update(source.status())
    return lines
