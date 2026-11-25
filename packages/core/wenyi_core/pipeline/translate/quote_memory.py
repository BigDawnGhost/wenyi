"""Book-wide verbatim memory for repeated quotations, verse and epigraphs.

Detect non-trivial source spans (whole verse lines, quoted passages, or repeated aphoristic
clauses) that recur in two or more distinct paragraphs. Supports:
- Thread-safe ``clone()`` for parallel chapter waves so intra-chapter repeats work live.
- ``cross_chapter_spans()`` + ``seed_canonical()`` so cross-chapter repeated quotes can be
  pre-rendered once in compiled mode and shared identically across all parallel workers.
- Adaptive minimum length (10 normalized chars for CJK/Japanese, 24 for Latin scripts) so
  trivial dialogue like "Come with me" is never treated as a verbatim callback.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Sequence

_MIN_CJK_SPAN_CHARS = 10
_MIN_LATIN_SPAN_CHARS = 24
_CJK_OR_KANA_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uf900-\ufaff]")
_QUOTED_SPAN_RE = re.compile(r"[「『“\"«„]([^「」『』“”\"«»„]{10,240})[」』”\"»“]")
# Strip whitespace and quote/bracket wrappers while keeping meaning-bearing punctuation
# (apostrophes, commas, sentence marks) significant so "We'll..." != "Well,...".
_STRIP_PUNCT_RE = re.compile(r"[\s\"“”「」『』«»„（）()【】\[\]]+")


def _normalize_span(text: str) -> str:
    return _STRIP_PUNCT_RE.sub("", unicodedata.normalize("NFKC", text)).casefold()


def _meets_min_length(norm_key: str) -> bool:
    if _CJK_OR_KANA_RE.search(norm_key):
        return len(norm_key) >= _MIN_CJK_SPAN_CHARS
    return len(norm_key) >= _MIN_LATIN_SPAN_CHARS


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?])\s*")


def extract_candidate_spans(source: str) -> list[str]:
    """Return candidate quotable spans (whole short passage, inner quotes, and sub-sentences)."""
    stripped = source.strip()
    if not stripped:
        return []
    spans: list[str] = []
    seen: set[str] = set()

    def _add(span: str) -> None:
        key = _normalize_span(span)
        if _meets_min_length(key) and key not in seen:
            spans.append(span)
            seen.add(key)

    if len(stripped) <= 240:
        _add(stripped)
    for match in _QUOTED_SPAN_RE.finditer(stripped):
        inner = match.group(1).strip()
        _add(inner)
        # Also index non-trivial sub-sentences inside a longer quoted poem/passage so a later
        # callback quoting just the second half still hits the same key.
        for part in _SENTENCE_SPLIT_RE.split(inner):
            part = part.strip()
            if part and part != inner:
                _add(part)
    return spans


class QuoteMemory:
    """Track canonical translations for repeated source quotations and verse across a book."""

    def __init__(
        self,
        repeated_keys: set[str],
        *,
        cross_chapter_examples: dict[str, str] | None = None,
    ):
        self._repeated_keys = repeated_keys
        self._cross_chapter_examples: dict[str, str] = dict(cross_chapter_examples or {})
        # normalized source key -> (earliest position, canonical source, canonical target)
        # Prompt references (may include whole-paragraph, paired inner quotes, or parent-quote refs).
        self._canonical: dict[str, tuple[tuple[int, int], str, str]] = {}
        # Strict whole-paragraph exact matches only (safe for deterministic replacement).

    @classmethod
    def from_chapters(cls, chapters: Sequence[tuple[int, Sequence[str]]]) -> QuoteMemory:
        """Index spans that appear in at least two distinct paragraphs across the book."""
        occurrences: dict[str, set[tuple[int, int]]] = defaultdict(set)
        chapters_by_key: dict[str, set[int]] = defaultdict(set)
        first_span_by_key: dict[str, str] = {}
        first_pos_by_key: dict[str, tuple[int, int]] = {}
        for chapter_index, sources in chapters:
            for seg_index, source in enumerate(sources):
                pos = (chapter_index, seg_index)
                for span in extract_candidate_spans(source):
                    key = _normalize_span(span)
                    if _meets_min_length(key):
                        occurrences[key].add(pos)
                        chapters_by_key[key].add(chapter_index)
                        first_span_by_key.setdefault(key, span)
                        if key not in first_pos_by_key or pos < first_pos_by_key[key]:
                            first_pos_by_key[key] = pos
        repeated = {key for key, positions in occurrences.items() if len(positions) >= 2}
        # Sort cross-chapter spans deterministically by earliest book position (then key) so
        # truncation at max_items=15 is independent of Python hash randomization.
        cross_keys = sorted(
            (key for key in repeated if len(chapters_by_key[key]) >= 2),
            key=lambda k: (first_pos_by_key[k], k),
        )
        cross_chapter = {key: first_span_by_key[key] for key in cross_keys}
        return cls(repeated, cross_chapter_examples=cross_chapter)

    def clone(self) -> QuoteMemory:
        """Return an independent copy safe for wave-local updates inside a worker thread."""
        copy = QuoteMemory(
            set(self._repeated_keys),
            cross_chapter_examples=self._cross_chapter_examples,
        )
        copy._canonical = dict(self._canonical)
        return copy

    @property
    def repeated_count(self) -> int:
        return len(self._repeated_keys)

    def cross_chapter_spans(
        self, *, max_items: int = 15, include_canonical: bool = False
    ) -> list[str]:
        """Return raw source spans that recur across >=2 distinct chapters (for pre-rendering).

        ``include_canonical`` also returns spans whose canonical rendering was already
        observed or pre-seeded, so whole-book snapshots stay identical on resume runs where
        earlier chapters were already translated.
        """
        return [
            span
            for key, span in self._cross_chapter_examples.items()
            if include_canonical or key not in self._canonical
        ][:max_items]

    def seed_canonical(self, source_span: str, target_rendering: str) -> None:
        """Pre-seed a canonical rendering (position (-1, -1)) before parallel waves start."""
        cleaned = (target_rendering or "").strip()
        stripped = (source_span or "").strip()
        if not cleaned or not stripped:
            return
        key = _normalize_span(stripped)
        if key in self._repeated_keys and key not in self._canonical:
            self._canonical[key] = ((-1, -1), stripped, cleaned)

    def _record_entry(
        self,
        table: dict[str, tuple[tuple[int, int], str, str]],
        key: str,
        position: tuple[int, int],
        span: str,
        stored_target: str,
        *,
        prefer_update: bool,
    ) -> None:
        existing = table.get(key)
        if (
            existing is None
            or position < existing[0]
            or (prefer_update and position == existing[0])
        ):
            table[key] = (position, span, stored_target)

    def observe(
        self,
        position: tuple[int, int],
        source: str,
        target: str,
        *,
        prefer_update: bool = False,
    ) -> None:
        """Record the earliest unambiguous rendering for any repeated span in ``source``."""
        cleaned_target = (target or "").strip()
        stripped_source = (source or "").strip()
        if not cleaned_target or not stripped_source or not self._repeated_keys:
            return

        # 1. Whole-paragraph match.
        if len(stripped_source) <= 240:
            whole_key = _normalize_span(stripped_source)
            if whole_key in self._repeated_keys:
                self._record_entry(
                    self._canonical,
                    whole_key,
                    position,
                    stripped_source,
                    cleaned_target,
                    prefer_update=prefer_update,
                )

        # 2. Inner quoted spans: only pair when source and target quote counts match 1-to-1
        # so multi-quote paragraphs never cross-assign unrelated speech/verse spans.
        src_quotes = [m.group(1).strip() for m in _QUOTED_SPAN_RE.finditer(stripped_source)]
        tgt_quotes = [m.group(1).strip() for m in _QUOTED_SPAN_RE.finditer(cleaned_target)]
        if not src_quotes or len(src_quotes) != len(tgt_quotes):
            return

        for src_q, tgt_q in zip(src_quotes, tgt_quotes):
            q_key = _normalize_span(src_q)
            if q_key in self._repeated_keys:
                self._record_entry(
                    self._canonical,
                    q_key,
                    position,
                    src_q,
                    tgt_q,
                    prefer_update=prefer_update,
                )
            # Sub-sentences inside a paired quote (e.g. second stanza of a lyric repeated later):
            # align sentence-by-sentence when counts match, otherwise keep as prompt-only reference
            # under the parent quote wording.
            src_parts = [p.strip() for p in _SENTENCE_SPLIT_RE.split(src_q) if p.strip()]
            if len(src_parts) <= 1:
                continue
            tgt_parts = [p.strip() for p in re.split(r"(?<=[.!?。！？])\s+", tgt_q) if p.strip()]
            for idx, part in enumerate(src_parts):
                p_key = _normalize_span(part)
                if p_key not in self._repeated_keys:
                    continue
                if len(tgt_parts) == len(src_parts):
                    self._record_entry(
                        self._canonical,
                        p_key,
                        position,
                        part,
                        tgt_parts[idx],
                        prefer_update=prefer_update,
                    )
                else:
                    # Still record the parent quote as a read-only prompt reference.
                    self._record_entry(
                        self._canonical,
                        p_key,
                        position,
                        src_q,
                        tgt_q,
                        prefer_update=prefer_update,
                    )

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
            "[Repeated quotation reference — reuse the established wording consistently when the same verse or quote recurs]"
        ]
        for src, tgt in refs:
            lines.append(f"- {src} => {tgt}")
        return "\n".join(lines)
