"""Track how often finished translations use each automatically extracted glossary target.

A mapping the translator keeps overriding is evidence that it is wrong or depends on
context: 孤 stored as "lone" or 大人 as "Your Excellency" appeared in about a third of the
paragraphs containing them, while names appeared in all of them. Such entries are left out
of later prompts instead of being forced on every occurrence. The counts are derived only
from saved source/draft pairs, so a resumed run recomputes the same decision.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from ..models.glossary import STATUS_CONTEXT_DEPENDENT, GlossaryTerm
from .store import _LEADING_ARTICLE, _source_pattern, normalize_match_text

MIN_OCCURRENCES = 5
MAX_ADHERENCE = 0.35


def _key(text: str) -> str:
    return " ".join(normalize_match_text(text).split())


def _matcher(key: str) -> re.Pattern[str] | str:
    """Word boundaries for spaced scripts, plain substring for CJK and other unspaced scripts."""
    return _source_pattern(key) or key


def _found(matcher: re.Pattern[str] | str, text: str) -> bool:
    return matcher in text if isinstance(matcher, str) else matcher.search(text) is not None


@dataclass
class _Counts:
    target: str
    started: bool = False
    seen: int = 0
    used: int = 0


@dataclass
class GlossaryAdherence:
    """Count, per extracted term, paragraphs that contain its source and use its target.

    Counting starts at the first paragraph that uses the current target. Paragraphs written
    before then could not follow it, as when a user edits a target the earlier text predates.
    """

    _pairs: list[tuple[str, str]] = field(default_factory=list)
    _counts: dict[str, _Counts] = field(default_factory=dict)
    _scanned: dict[str, int] = field(default_factory=dict)

    def observe(self, pairs: Iterable[tuple[str, str]]) -> None:
        """Add translated paragraphs as ``(source, draft)`` pairs; blank drafts are ignored."""
        self._pairs.extend(
            (_key(source), _key(target)) for source, target in pairs if target.strip()
        )

    def _update(self, term: GlossaryTerm) -> _Counts:
        counts = self._counts.get(term.source)
        if counts is None or counts.target != term.target:
            # A changed target is a new mapping; earlier paragraphs are rescanned for it.
            counts = self._counts[term.source] = _Counts(term.target)
            self._scanned[term.source] = 0
        start = self._scanned[term.source]
        if start < len(self._pairs):
            source_key = _key(term.source)
            target_key = _LEADING_ARTICLE.sub("", _key(term.target))
            # Aliases are not counted: a hero called by an epithet (大圣 "the Great Sage") must
            # not make the main name look overridden.
            source = _matcher(source_key) if source_key else None
            target = _matcher(target_key) if target_key else None
            for source_text, target_text in self._pairs[start:]:
                if source is None or not _found(source, source_text):
                    continue
                used = target is not None and _found(target, target_text)
                counts.started = counts.started or used
                if counts.started:
                    counts.seen += 1
                    counts.used += used
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
        return counts.seen >= MIN_OCCURRENCES and counts.used / counts.seen < MAX_ADHERENCE

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
