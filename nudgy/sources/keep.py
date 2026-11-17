"""Google Keep notes, through the official Keep API (Workspace accounts only)."""
from __future__ import annotations

from .. import keep
from ..model import Item
from .base import Round, Source


class Keep(Source):
    group = "keep"
    order = 30

    def fetch(self, round: Round) -> dict[str, list[Item]]:
        notes = keep.fetch_keep(round.now)
        return {} if notes is None else {"keep": notes}

    def status(self) -> dict[str, str]:
        return {"keep": keep.status()}

    def explain(self, error: Exception) -> str:
        return str(error) if isinstance(error, keep.KeepRefused) else super().explain(error)


SOURCE = Keep()
