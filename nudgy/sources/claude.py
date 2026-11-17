"""Follow-ups in your Markdown notes (the Claude notes folder in settings)."""
from __future__ import annotations

from .. import claude_notes
from ..model import Item
from .base import Round, Source


class ClaudeNotes(Source):
    group = "claude"
    order = 40

    def fetch(self, round: Round) -> dict[str, list[Item]]:
        return {"claude": claude_notes.read(round.now)}


SOURCE = ClaudeNotes()
