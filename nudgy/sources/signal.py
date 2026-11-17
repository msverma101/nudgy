"""Signal chats waiting on you, through signal-cli (linked as a secondary device)."""
from __future__ import annotations

from .. import chats
from ..model import Item
from .base import Round, Source


class Signal(Source):
    group = "signal"
    order = 20

    def fetch(self, round: Round) -> dict[str, list[Item]]:
        result = chats.fetch_signal(round.now)
        return {} if result is None else {"signal": result}     # None: not linked

    def status(self) -> dict[str, str]:
        account = chats.signal_account()
        return {"signal": f"Linked ({account})" if account else
                "Not linked" if chats.signal_cli() else "signal-cli not installed"}

    def explain(self, error: Exception) -> str:
        return str(error) if isinstance(error, chats.NotReady) else super().explain(error)


SOURCE = Signal()
