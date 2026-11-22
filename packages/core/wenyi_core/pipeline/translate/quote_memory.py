"""Book-wide verbatim memory for repeated quotations, verse and epigraphs.

Detect non-trivial source spans (whole verse lines or quoted passages of at least 12
characters) that recur in two or more distinct paragraphs. Once the earliest occurrence
is translated, supply that canonical rendering as a compact reference to later batches
and optionally reuse it verbatim so repeated poems, letters and epigraphs never drift.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Sequence

_MIN_SPAN_CHARS = 12
_QUOTED_SPAN_RE = re.compile(r"[「『“\"]([^「」『』“”\"]{12,240})[」』”\"]")
# Drop only spacing and the quote/bracket wrappers that vary between occurrences; keep
# meaning-bearing punctuation so "We'll go" and "Well, go" never share a key.
_STRIP_WRAPPER_RE = re.compile(r"[\s\"“”「」『』（）()【】\[\]]+")


def _normalize_span(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).replace("’", "'").replace("‘", "'")
    return _STRIP_WRAPPER_RE.sub("", normalized).casefold()


def extract_candidate_spans(source: str) -> list[str]:
    """Return candidate quotable spans (whole short passage or inner quotes) from one paragraph."""
    stripped = source.strip()
    if not stripped:
        return []
    spans: list[str] = []
    if len(_normalize_span(stripped)) >= _MIN_SPAN_CHARS and len(stripped) <= 240:
        spans.append(stripped)
    for match in _QUOTED_SPAN_RE.finditer(stripped):
        inner = match.group(1).strip()
        if len(_normalize_span(inner)) >= _MIN_SPAN_CHARS and inner != stripped:
            spans.append(inner)
    return spans


class QuoteMemory:
    """Track canonical translations for repeated source quotations and verse across a book."""

    def __init__(self, repeated_keys: set[str]):
        self._repeated_keys = repeated_keys
        # normalized source key -> (earliest position, canonical source, canonical target)
        self._canonical: dict[str, tuple[tuple[int, int], str, str]] = {}

    @classmethod
    def from_chapters(cls, chapters: Sequence[tuple[int, Sequence[str]]]) -> QuoteMemory:
        """Index spans that appear in at least two distinct paragraphs across the book."""
        occurrences: dict[str, set[tuple[int, int]]] = defaultdict(set)
        for chapter_index, sources in chapters:
            for seg_index, source in enumerate(sources):
                pos = (chapter_index, seg_index)
                for span in extract_candidate_spans(source):
                    key = _normalize_span(span)
                    if len(key) >= _MIN_SPAN_CHARS:
                        occurrences[key].add(pos)
        repeated = {key for key, positions in occurrences.items() if len(positions) >= 2}
        return cls(repeated)

    @property
    def repeated_count(self) -> int:
        return len(self._repeated_keys)

    def observe(self, position: tuple[int, int], source: str, target: str) -> None:
        """Record the earliest translated rendering for any repeated span present in ``source``."""
        cleaned_target = (target or "").strip()
        stripped_source = (source or "").strip()
        if not cleaned_target or not self._repeated_keys:
            return
        quoted_targets = [m.group(1).strip() for m in _QUOTED_SPAN_RE.finditer(cleaned_target)]
        for span in extract_candidate_spans(stripped_source):
            key = _normalize_span(span)
            if key not in self._repeated_keys:
                continue
            if span == stripped_source:
                stored_target = cleaned_target
            elif len(quoted_targets) == 1:
                stored_target = quoted_targets[0]
            else:
                # Without one identifiable target quote, the paragraph is not the quote.
                continue
            existing = self._canonical.get(key)
            if existing is None or position < existing[0]:
                self._canonical[key] = (position, span, stored_target)

    def exact_match(self, source: str) -> str | None:
        """Return a canonical full-paragraph target if ``source`` is an exact repeat of one."""
        stripped = source.strip()
        if not stripped or not self._repeated_keys:
            return None
        key = _normalize_span(stripped)
        entry = self._canonical.get(key)
        if entry is None:
            return None
        _, canonical_src, canonical_tgt = entry
        if _normalize_span(canonical_src) == key and len(stripped) <= 240:
            return canonical_tgt
        return None

    def references_for_batch(self, sources: Sequence[str]) -> list[tuple[str, str]]:
        """Return ``(source_span, established_target)`` pairs relevant to a batch."""
        if not self._canonical:
            return []
        seen: set[str] = set()
        refs: list[tuple[str, str]] = []
        for source in sources:
            for span in extract_candidate_spans(source):
                key = _normalize_span(span)
                if key in seen or key not in self._canonical:
                    continue
                seen.add(key)
                _, canonical_src, canonical_tgt = self._canonical[key]
                refs.append((canonical_src, canonical_tgt))
        return refs

    def format_prompt_block(self, sources: Sequence[str], *, max_items: int = 5) -> str:
        """Render compact verbatim callback guidance for any repeated quotes in ``sources``."""
        refs = self.references_for_batch(sources)[:max_items]
        if not refs:
            return ""
        lines = [
            "[Repeated quotation reference — reuse the established wording verbatim when the same verse or quote recurs]"
        ]
        for src, tgt in refs:
            lines.append(f"- {src} => {tgt}")
        return "\n".join(lines)
