"""Track how often finished translations use each automatically extracted glossary target.

A mapping the translator keeps overriding is evidence that it is wrong or depends on
context: 孤 stored as "lone" or 大人 as "Your Excellency" appeared in about a third of the
paragraphs containing them, while names appeared in all of them. Such entries are left out
of later prompts instead of being forced on every occurrence. The counts are derived only
from saved source/target pairs, so a resumed run recomputes the same decision.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from ..models.glossary import STATUS_CONTEXT_DEPENDENT, GlossaryTerm
from .store import normalize_match_text, source_matches_text, term_match_sources

MIN_OCCURRENCES = 5
MAX_ADHERENCE = 0.35

_LEADING_ARTICLE = re.compile(r"^the\s+")


def _target_key(target: str) -> str:
    return _LEADING_ARTICLE.sub("", " ".join(normalize_match_text(target).split()))


def _target_pattern(key: str) -> re.Pattern[str]:
    # Letters on either side mean the target is part of a longer word ("Lu" in "Lushan").
    return re.compile(r"(?<![^\W\d_])" + re.escape(key) + r"(?![^\W\d_])")


@dataclass
class _Counts:
    target: str
    seen: int = 0
    used: int = 0


@dataclass
class GlossaryAdherence:
    """Count, per extracted term, paragraphs that contain its source and use its target."""

    min_occurrences: int = MIN_OCCURRENCES
    max_adherence: float = MAX_ADHERENCE
    _pairs: list[tuple[str, str]] = field(default_factory=list)
    _counts: dict[str, _Counts] = field(default_factory=dict)
    _scanned: dict[str, int] = field(default_factory=dict)

    def observe(self, pairs: Iterable[tuple[str, str]]) -> None:
        """Add translated paragraphs as ``(source, target)`` pairs; blank targets are ignored."""
        self._pairs.extend(
            (source, " ".join(normalize_match_text(target).split()))
            for source, target in pairs
            if target.strip()
        )

    def _update(self, term: GlossaryTerm) -> _Counts:
        counts = self._counts.get(term.source)
        if counts is None or counts.target != term.target:
            # A changed target is a new mapping; earlier paragraphs are rescanned for it.
            counts = self._counts[term.source] = _Counts(term.target)
            self._scanned[term.source] = 0
        start = self._scanned[term.source]
        if start < len(self._pairs):
            key = _target_key(term.target)
            pattern = _target_pattern(key) if key else None
            sources = term_match_sources(term)
            for source, target in self._pairs[start:]:
                if any(source_matches_text(spelling, source) for spelling in sources):
                    counts.seen += 1
                    if pattern is not None and pattern.search(target):
                        counts.used += 1
            self._scanned[term.source] = len(self._pairs)
        return counts

    def ignored(self, term: GlossaryTerm) -> bool:
        """Return True when the translator has overridden an extracted mapping too often.

        Only extracted terms qualify (they carry ``first_chapter``); imported and manually
        added entries are user requirements and are always sent. Context-dependent entries
        already ask the translator to choose by context, so they are always sent too.
        """
        if term.first_chapter is None or term.status == STATUS_CONTEXT_DEPENDENT:
            return False
        counts = self._update(term)
        return (
            counts.seen >= self.min_occurrences and counts.used / counts.seen < self.max_adherence
        )

    def filter(self, terms: list[GlossaryTerm]) -> tuple[list[GlossaryTerm], list[str]]:
        """Return the terms to send, in order, and the sources left out."""
        kept: list[GlossaryTerm] = []
        left_out: list[str] = []
        for term in terms:
            if self.ignored(term):
                left_out.append(term.source)
            else:
                kept.append(term)
        return kept, left_out


__all__ = ["MAX_ADHERENCE", "MIN_OCCURRENCES", "GlossaryAdherence"]
