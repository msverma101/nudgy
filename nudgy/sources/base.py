"""What every source plugin looks like.

A source is one file in this folder with a `SOURCE` object: the group it belongs to (what the change
check names when something new arrived), how to fetch its items, the lines it shows in Accounts, and
how its errors read. One source failing never stops the others.

To add a source: copy `signal.py`, change the four parts, and add its name to `PLUGINS` in
`__init__.py`. Nothing in the window, the pet or the store has to change.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .. import google
from ..model import Item


@dataclass
class Round:
    """One sync round, shared by the sources in order (Luma uses the Calendar fetched before it)."""
    now: datetime
    items: dict[str, list[Item]] = field(default_factory=dict)       # fetched so far
    errors: dict[str, str] = field(default_factory=dict)
    calendar: list[Item] | None = None                                 # last good Calendar list

    def known_calendar(self) -> list[Item]:
        return self.items.get("calendar", self.calendar or [])


class Source:
    group = ""                  # name the change check and `nudgy sync` use
    order = 50                  # lower runs first

    def fetch(self, round: Round) -> dict[str, list[Item]]:
        """Items per kind (one source may bring several, like Gmail and Calendar). Raise on failure;
        leave a kind out when it's switched off or not linked."""
        raise NotImplementedError

    def status(self) -> dict[str, str]:
        """Lines for the Accounts window: {service: state}."""
        return {}

    def explain(self, error: Exception) -> str:
        """The error in words for the status line."""
        return google.describe(error)
