"""Translate aligned title batches without accessing book state."""

from ..errors import WenyiError
from ..llm.json_parser import UnsafeJsonRepairError
from . import prompts
from .base import Agent


class TitleOutputError(WenyiError, RuntimeError):
    """Expose response counts so the caller can retain its rejection event contract."""

    def __init__(self, expected: int, actual: int | None):
        self.expected = expected
        self.actual = actual
        super().__init__(
            "Chapter/TOC title translation returned an invalid number of items: "
            f"expected {expected}, got {actual if actual is not None else 'non-list'}"
        )


class TitleTranslator(Agent):
    """Own title prompts, the existing operation route and response alignment."""

    def translate(self, titles: list[str], glossary_text: str) -> list[str]:
        """Return stripped titles; empty-string fallback belongs to the title plan."""
        system = self.render("title_translator_system", src=self.src, tgt=self.tgt, n=len(titles))
        user = self.render(
            "title_translator_user",
            src=self.src,
            tgt=self.tgt,
            glossary=glossary_text,
            n=len(titles),
            numbered_titles=prompts.numbered(titles),
        )
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        try:
            data, _raw = self._complete_json_turn(
                messages, operation="translation.title", require_complete_payload=True
            )
        except UnsafeJsonRepairError:
            # Repair can fold stray reasoning into a title while keeping the count; sample once
            # more and let a second unsafe reply stop the title run like any unusable JSON.
            data, _raw = self._complete_json_turn(
                messages, operation="translation.title", require_complete_payload=True
            )
        out = data.get("titles") if isinstance(data, dict) else data
        if not isinstance(out, list) or len(out) != len(titles):
            raise TitleOutputError(len(titles), len(out) if isinstance(out, list) else None)
        return [str(title).strip() for title in out]
