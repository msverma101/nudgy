"""Gmail, Google Calendar, Google Tasks and LinkedIn (read from LinkedIn's emails in Gmail)."""
from __future__ import annotations

from .. import google as api
from ..model import Item
from .base import Round, Source


class Google(Source):
    group = "google"
    order = 10                          # first: Luma drops events already in Calendar

    def fetch(self, round: Round) -> dict[str, list[Item]]:
        items, errors = api.sync_all(round.now)      # each Google service fails on its own
        round.errors.update(errors)
        return items

    def status(self) -> dict[str, str]:
        signed_in = api.token_path().exists()
        return {"google": "Signed in" if signed_in else "Signed out" if api.client_path().exists() else "Not set up",
                "linkedin": "Through Gmail" if signed_in else "Needs Google"}


SOURCE = Google()
