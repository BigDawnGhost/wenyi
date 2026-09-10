"""Polishing agent using the strong tier.
Improve literary quality in the target language without changing information or paragraph
count. Preserve the original translation on alignment failure so polishing cannot drop
paragraphs.
"""

from __future__ import annotations

from ..glossary.store import GlossaryTerm
from ..i18n.prompts import render
from . import prompts
from .base import Agent


class Polisher(Agent):
    def polish(
        self,
        targets: list[str],
        *,
        glossary_terms: list[GlossaryTerm] | None = None,
        style: str = "",
        next_source: str = "",
    ) -> list[str]:
        """Polish an aligned list; return the input unchanged on call or length failure."""
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
        return list(
            targets
        )  # Preserve the original translation on failure or paragraph-count mismatch.
