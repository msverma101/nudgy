"""Luma events you registered for, and public events that fit you (the event finder)."""
from __future__ import annotations

import logging
from datetime import timedelta

from .. import discover, google, luma
from ..model import Item
from .base import Round, Source

log = logging.getLogger(__name__)


class Events(Source):
    group = "events"
    order = 60                          # after Google: needs this round's Calendar

    def fetch(self, round: Round) -> dict[str, list[Item]]:
        items: dict[str, list[Item]] = {}
        known_calendar = round.known_calendar()
        everything = None
        try:
            everything = luma.fetch_luma(round.now, days=discover.DAYS_AHEAD)
            horizon = (round.now + timedelta(days=luma.days_ahead())).date().isoformat()
            if everything is not None:
                events = [e for e in everything if e.due and e.due[:10] <= horizon]
                items["luma"] = luma.drop_duplicates(events, known_calendar)
        except Exception as error:      # the finder below still runs
            log.warning("luma failed: %s", google.describe(error))
            round.errors["luma"] = str(error) if isinstance(error, ValueError) else google.describe(error)
        try:
            # Public events across Europe that fit you, minus the ones you are already going to.
            found, applied = discover.fetch(round.now, applied=google.luma_applications)
            items["potential"] = discover.drop_known(found, (everything or []) + known_calendar, applied)
        except Exception as error:
            log.warning("event finder failed: %s", google.describe(error))
            round.errors["potential"] = google.describe(error)
        return items

    def status(self) -> dict[str, str]:
        return {"luma": "Connected" if luma.feed_url() else "Not connected"}


SOURCE = Events()
