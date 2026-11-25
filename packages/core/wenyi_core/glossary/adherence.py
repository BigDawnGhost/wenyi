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


# Inflected targets decline names and terms ("Москва" -> "в Москве", "Алиса" -> "Алисой",
# "Лунный лес" -> "в Лунном лесу"), so a target word counts as used when it appears with a
# changed final letter or two and a short ending. The stem always keeps all but the last one
# or two letters, so "court" never matches "could" and "force" never matches "for". A false
# hit only keeps a mapping in prompts longer; a false miss would drop a correct one.


def _word_pattern(word: str) -> str:
    if not word[-1:].isalpha():
        return re.escape(word)
    if len(word) >= 6:
        # "church" -> "churches", "дракон" -> "драконом", "медведь" -> "медведями".
        return re.escape(word[:-2]) + r"\w{1,4}"
    if len(word) >= 4:
        # "Алиса" -> "Алисой", "Волга" -> "Волге", "Анна" -> "Анну"; never "king" -> "kingdom".
        return re.escape(word[:-1]) + r"\w{1,3}"
    # Very short words keep their full form: "лес" -> "лесом", but "Lu" never "Lushan".
    return re.escape(word) + rf"\w{{0,{min(2, len(word) - 1)}}}"


def _inflection_tolerant_matcher(key: str) -> re.Pattern[str] | str:
    """Match each target word with a short inflectional ending in spaced scripts.

    Unspaced scripts (CJK) keep the plain substring match, which already ignores endings.
    """
    if _source_pattern(key) is None:
        return key
    left = r"(?<!\w)" if key[:1].isalnum() else ""
    right = r"(?!\w)" if key[-1:].isalnum() else ""
    return re.compile(left + r"\s+".join(_word_pattern(w) for w in key.split()) + right)


def _found(matcher: re.Pattern[str] | str, text: str) -> bool:
    return matcher in text if isinstance(matcher, str) else matcher.search(text) is not None


Position = tuple[int, int]


@dataclass
class _Counts:
    target: str
    # Book position (chapter, paragraph) of each paragraph containing the source -> used.
    hits: dict[Position, bool] = field(default_factory=dict)


@dataclass
class GlossaryAdherence:
    """Count, per extracted term, paragraphs that contain its source and use its target.

    Counting starts at the earliest paragraph, in book order, that uses the current target.
    Paragraphs before it could not follow it, as when a user edits a target the earlier
    text predates. Positions, not arrival order, decide, so translating one chapter first
    and resuming later reach the same result.
    """

    _pairs: list[tuple[Position, str, str]] = field(default_factory=list)
    _counts: dict[str, _Counts] = field(default_factory=dict)
    _scanned: dict[str, int] = field(default_factory=dict)

    def observe(self, pairs: Iterable[tuple[Position, str, str]]) -> None:
        """Add translated paragraphs as ``(position, source, draft)``; blank drafts are ignored.

        A position seen again replaces the earlier paragraph there.
        """
        self._pairs.extend(
            (position, _key(source), _key(target))
            for position, source, target in pairs
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
            source_key = _key(term.source)
            target_key = _LEADING_ARTICLE.sub("", _key(term.target))
            # Aliases are not counted: a hero called by an epithet (大圣 "the Great Sage") must
            # not make the main name look overridden.
            source = _matcher(source_key) if source_key else None
            target = _inflection_tolerant_matcher(target_key) if target_key else None
            for position, source_text, target_text in self._pairs[start:]:
                if source is not None and _found(source, source_text):
                    counts.hits[position] = target is not None and _found(target, target_text)
                else:
                    counts.hits.pop(position, None)
            self._scanned[term.source] = len(self._pairs)
        return counts

    @staticmethod
    def _rate(counts: _Counts) -> tuple[int, int]:
        used_at = [position for position, used in counts.hits.items() if used]
        if not used_at:
            return 0, 0
        first = min(used_at)
        window = [used for position, used in counts.hits.items() if position >= first]
        return len(window), sum(window)

    def ignored(self, term: GlossaryTerm) -> bool:
        """Return True when the translator has overridden an extracted mapping too often.

        Only extracted terms qualify (they carry ``first_chapter``); imported, manually
        added, edited and resolved entries are user requirements and are always sent. Context-dependent entries
        already ask the translator to choose by context, so they are always sent too.
        """
        if term.first_chapter is None or term.status == STATUS_CONTEXT_DEPENDENT:
            return False
        seen, used = self._rate(self._update(term))
        return seen >= MIN_OCCURRENCES and used / seen < MAX_ADHERENCE

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
