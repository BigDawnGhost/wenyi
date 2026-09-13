"""Polishing agent using the strong tier.
Improve literary quality in the target language without changing information or paragraph
count. Prefer appending a polish user turn to the translation conversation so shared
prompt prefixes stay cacheable. Preserve the original translation on alignment failure so
polishing cannot drop paragraphs.
"""

from __future__ import annotations

from ..glossary.store import GlossaryTerm
from ..i18n.prompts import render
from . import prompts
from .base import Agent, Messages


class Polisher(Agent):
    def polish(
        self,
        targets: list[str],
        *,
        glossary_terms: list[GlossaryTerm] | None = None,
        style: str = "",
        next_source: str = "",
    ) -> list[str]:
        """Polish an aligned list in a fresh conversation; return input unchanged on failure."""
        if not targets:
            return []
        n = len(targets)
        system = render("polisher_system", src=self.src, tgt=self.tgt, n=n)
        user = render(
            "polisher_user",
            src=self.src,
            tgt=self.tgt,
            glossary=prompts.render_glossary(glossary_terms or []),
            style=style or "(none)",
            n=n,
            numbered_target=prompts.numbered(targets),
            next_source=prompts.render_source_reference(next_source),
        )
        items = self._ask_json(system, user, operation="polish.body", key="polished", default=None)
        if isinstance(items, list) and len(items) == n:
            return [str(x) for x in items]
        return list(targets)

    def polish_continue(
        self,
        turn: Messages,
        *,
        n: int,
        next_source: str = "",
    ) -> list[str] | None:
        """Append a polish user turn to a successful translation transcript.

        Returns the polished list on success, or ``None`` when the continuation fails so the
        caller can fall back to a standalone polish call or keep the raw translations.
        """
        if n <= 0 or len(turn) < 3:
            return None
        continue_user = render(
            "polisher_continue_user",
            src=self.src,
            tgt=self.tgt,
            n=n,
            next_source=prompts.render_source_reference(next_source),
        )
        messages: Messages = [
            *turn,
            {"role": "user", "content": continue_user},
        ]
        items = self._ask_json_messages(
            messages,
            operation="polish.body",
            key="polished",
            default=None,
        )
        if isinstance(items, list) and len(items) == n:
            return [str(x) for x in items]
        return None
