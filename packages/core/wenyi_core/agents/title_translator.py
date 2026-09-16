"""Translate aligned title batches without accessing book state."""

from ..i18n.prompts import render
from . import prompts
from .base import Agent


class TitleOutputError(RuntimeError):
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
        system = render("title_translator_system", src=self.src, tgt=self.tgt, n=len(titles))
        user = render(
            "title_translator_user",
            src=self.src,
            tgt=self.tgt,
            glossary=glossary_text,
            n=len(titles),
            numbered_titles=prompts.numbered(titles),
        )
        data = self.client.complete_json(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            operation="translation.title",
        )
        out = data.get("titles") if isinstance(data, dict) else data
        if not isinstance(out, list) or len(out) != len(titles):
            raise TitleOutputError(len(titles), len(out) if isinstance(out, list) else None)
        return [str(title).strip() for title in out]
