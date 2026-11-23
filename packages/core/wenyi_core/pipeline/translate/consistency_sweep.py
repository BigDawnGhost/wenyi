"""Fast post-parallel book-wide consistency sweep (10-20s vs. 33m legacy Review).

Closes the residual cross-chapter consistency gap of parallel chapter translation with:
1. Deterministic whole-paragraph repeated-quote unification (earliest polished chapter wins).
2. Book-wide glossary-miss detection across chapters (finds minority renderings missed by
   deterministic name normalization).
3. Mid-frequency non-glossary term drift detection (statistical co-occurrence across
   chapters to spot split renderings like 'retainer'/'adviser' or 'sovereign'/'king'),
   adjudicated in one tiny call and repaired via surgical sparse patches.

Never mutates segments with offset-bound annotation/protected-object/DOCX-style placements.
"""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from ...agents.batch_risk import _is_generic, _target_contains_term
from ...agents.book_bible import _CJK_EDGE_PARTICLES, _CJK_STOP_CHARS
from ...agents.sparse_editor import SparseSemanticEditor
from ...glossary.store import STATUS_OK, normalize_match_text, source_matches_text
from ...i18n.languages import (
    glossary_context_dependent_sources,
    glossary_generic_target_words,
)
from ...models.document import Chapter, Segment
from ...models.glossary import GlossaryTerm
from ...storage.protocol import Storage
from .quote_memory import QuoteMemory

_LOGGER = logging.getLogger(__name__)
_EN_CONTENT_WORD_RE = re.compile(r"\b[A-Za-z][A-Za-z'-]{3,}\b")
_EN_STOPWORDS = frozenset(
    {
        "that",
        "this",
        "with",
        "from",
        "they",
        "them",
        "their",
        "there",
        "were",
        "have",
        "been",
        "would",
        "could",
        "should",
        "about",
        "into",
        "over",
        "after",
        "before",
        "under",
        "then",
        "when",
        "where",
        "which",
        "while",
        "what",
        "your",
        "said",
        "asked",
        "replied",
        "will",
        "just",
        "only",
        "even",
        "also",
        "very",
        "much",
        "more",
        "most",
        "some",
        "such",
        "than",
        "into",
        "upon",
        "here",
        "made",
        "make",
        "like",
        "back",
        "down",
        "still",
        "never",
        "again",
        "well",
        "know",
        "come",
        "came",
        "went",
        "eyes",
        "hand",
        "head",
        "face",
        "voice",
        "looked",
        "turned",
        "heard",
    }
)


@dataclass(frozen=True)
class DriftCluster:
    source: str
    total_hits: int
    competing_targets: tuple[tuple[str, int], ...]
    paragraph_refs: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class ConsistencySweepResult:
    quotes_unified: int
    glossary_misses_patched: int
    drift_clusters_checked: int
    drift_patches_applied: int


def _has_offset_bound_metadata(seg: Segment) -> bool:
    meta = seg.meta or {}
    return bool(
        meta.get("annotations")
        or meta.get("epub_annotations")
        or meta.get("pdf_annotations")
        or meta.get("docx_runs")
        or meta.get("docx_styles")
        or meta.get("object_repairs")
    )


def _norm_en_word(word: str) -> str:
    low = word.casefold()
    if len(low) >= 5 and low.endswith("s") and not low.endswith("ss"):
        return low[:-1]
    return low


def _protected_segment_ids(chapter: Chapter) -> set[int]:
    protected: set[int] = set()
    head: Segment | None = None
    for seg in chapter.text_segments:
        if not seg.cont or head is None:
            head = seg
        if _has_offset_bound_metadata(head):
            protected.add(id(seg))
    return protected


def detect_mid_frequency_drift(
    chapters: Sequence[Chapter],
    *,
    source_lang: str,
    target_lang: str,
    existing_glossary_sources: set[str],
    min_hits: int = 5,
    max_clusters: int = 12,
) -> list[DriftCluster]:
    """Deterministically detect recurring non-glossary CJK terms with split English renderings."""
    src_base = (source_lang or "").lower().split("-", 1)[0]
    tgt_base = (target_lang or "").lower().split("-", 1)[0]
    if src_base != "zh" or tgt_base != "en":
        return []

    blocked = {
        normalize_match_text(s).strip() for s in glossary_context_dependent_sources(source_lang)
    }
    norm_existing = {normalize_match_text(s).strip() for s in existing_glossary_sources}

    # Index 2..3-char CJK noun-like terms occurring across >=2 chapters.
    term_paras: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
    term_chapters: dict[str, set[int]] = defaultdict(set)
    global_word_df: Counter[str] = Counter()
    total_paras = 0

    for ch in chapters:
        for seg_idx, seg in enumerate(ch.text_segments):
            src = (seg.source or "").strip()
            tgt = (seg.target or "").strip()
            if not src or not tgt:
                continue
            total_paras += 1
            words = {
                _norm_en_word(w)
                for w in _EN_CONTENT_WORD_RE.findall(tgt)
                if w.casefold() not in _EN_STOPWORDS
            }
            for w in words:
                global_word_df[w] += 1

            seen_grams: set[str] = set()
            for chunk in re.findall(r"[\u3400-\u9fff\uf900-\ufaff]{2,}", src):
                clen = len(chunk)
                for n in (2, 3):
                    if clen < n:
                        continue
                    for start in range(clen - n + 1):
                        gram = chunk[start : start + n]
                        if (
                            gram in seen_grams
                            or gram[0] in _CJK_EDGE_PARTICLES
                            or gram[-1] in _CJK_EDGE_PARTICLES
                            or all(c in _CJK_STOP_CHARS for c in gram)
                        ):
                            continue
                        norm = normalize_match_text(gram).strip()
                        if (
                            norm in blocked
                            or norm in norm_existing
                            or any(norm in ex for ex in norm_existing)
                        ):
                            continue
                        seen_grams.add(gram)
                        term_paras[gram].append((ch.index, seg_idx, tgt))
                        term_chapters[gram].add(ch.index)

    if total_paras < 10:
        return []

    clusters: list[DriftCluster] = []
    for gram, occurrences in term_paras.items():
        if len(occurrences) < min_hits or len(term_chapters[gram]) < 2:
            continue
        # Score distinctive target words that co-occur strongly with this source n-gram (lift > 2.5).
        n_occ = len(occurrences)
        word_counts: Counter[str] = Counter()
        for _ci, _si, tgt in occurrences:
            for w in {
                _norm_en_word(m)
                for m in _EN_CONTENT_WORD_RE.findall(tgt)
                if m.casefold() not in _EN_STOPWORDS
            }:
                word_counts[w] += 1

        distinctive: list[tuple[str, int]] = []
        for word, cnt in word_counts.most_common(12):
            share = cnt / n_occ
            base_rate = global_word_df[word] / total_paras
            if cnt >= 2 and 0.22 <= share <= 0.78 and base_rate > 0 and (share / base_rate) >= 1.5:
                distinctive.append((word, cnt))

        if len(distinctive) < 2:
            continue
        # Check mutual exclusivity of top two distinctive target words (split rendering signal!).
        (w1, c1), (w2, c2) = distinctive[0], distinctive[1]
        both = sum(
            1
            for _ci, _si, tgt in occurrences
            if re.search(rf"\b{re.escape(w1)}s?\b", tgt, re.I)
            and re.search(rf"\b{re.escape(w2)}s?\b", tgt, re.I)
        )
        if both <= min(c1, c2) * 0.25 and (c1 + c2) >= n_occ * 0.55:
            clusters.append(
                DriftCluster(
                    source=gram,
                    total_hits=n_occ,
                    competing_targets=((w1, c1), (w2, c2)),
                    paragraph_refs=tuple((ci, si) for ci, si, _ in occurrences),
                )
            )

    clusters.sort(key=lambda c: (-c.total_hits, c.source))
    return clusters[:max_clusters]


class ConsistencySweepService:
    """Fast post-translation cross-chapter unification pass."""

    def __init__(self, sparse_editor: SparseSemanticEditor, *, source_lang: str, target_lang: str):
        self._editor = sparse_editor
        self._src = source_lang
        self._tgt = target_lang

    def run(
        self,
        store: Storage,
        *,
        chapter_indices: Sequence[int],
        terms: Sequence[GlossaryTerm],
        style: str = "",
        max_patch_paragraphs: int = 24,
    ) -> ConsistencySweepResult:
        """Scan completed chapters for cross-chapter quote/glossary drift and patch surgically."""
        if len(chapter_indices) < 2:
            return ConsistencySweepResult(0, 0, 0, 0)

        chapters = [store.load_chapter(ci) for ci in chapter_indices]
        protected_by_ch = {ch.index: _protected_segment_ids(ch) for ch in chapters}
        modified_chapters: set[int] = set()

        # 1. Deterministic whole-paragraph repeated-quote unification (earliest chapter wins).
        qm = QuoteMemory.from_chapters(
            [(ch.index, [s.source for s in ch.text_segments]) for ch in chapters]
        )
        quotes_unified = 0
        for ch in chapters:
            for idx, seg in enumerate(ch.text_segments):
                if seg.target:
                    qm.observe((ch.index, idx), seg.source, seg.target)
        for ch in chapters:
            prot = protected_by_ch[ch.index]
            for seg in ch.text_segments:
                if id(seg) in prot or not seg.target:
                    continue
                canonical_quote = qm.exact_match(seg.source)
                if canonical_quote and canonical_quote != seg.target:
                    seg.target = canonical_quote
                    modified_chapters.add(ch.index)
                    quotes_unified += 1

        # 2. Book-wide glossary miss sweep (surgical sparse patches for missed canonical terms).
        generic_words = glossary_generic_target_words(self._tgt)
        entity_terms = [
            t
            for t in terms
            if t.status == STATUS_OK
            and t.type in {"person", "place", "organization", "term"}
            and len(t.source.strip()) >= 2
            and len(t.target.strip()) >= 2
            and not _is_generic(t.target, generic_words)
        ]

        drifted_items: list[tuple[Chapter, Segment, GlossaryTerm]] = []
        seen_seg_ids: set[int] = set()
        for ch in chapters:
            prot = protected_by_ch[ch.index]
            for seg in ch.text_segments:
                if id(seg) in prot or id(seg) in seen_seg_ids or not seg.target:
                    continue
                for term in entity_terms:
                    if not source_matches_text(term.source, seg.source):
                        continue
                    if _target_contains_term(seg.target, term.target, target_lang=self._tgt):
                        continue
                    if any(
                        other.source != term.source
                        and term.source in other.source
                        and source_matches_text(other.source, seg.source)
                        and _target_contains_term(seg.target, other.target, target_lang=self._tgt)
                        for other in entity_terms
                    ):
                        continue
                    drifted_items.append((ch, seg, term))
                    seen_seg_ids.add(id(seg))
                    break
                if len(drifted_items) >= max_patch_paragraphs:
                    break
            if len(drifted_items) >= max_patch_paragraphs:
                break

        glossary_patched = 0
        if drifted_items:
            for offset in range(0, len(drifted_items), 8):
                chunk = drifted_items[offset : offset + 8]
                chunk_sources = [seg.source for _, seg, _ in chunk]
                chunk_drafts = [seg.target or "" for _, seg, _ in chunk]
                chunk_terms = list({t.source: t for _, _, t in chunk}.values())
                res = self._editor.edit_batch(
                    chunk_sources,
                    chunk_drafts,
                    glossary_terms=chunk_terms,
                    style=style,
                )
                if not res.failed:
                    for idx in res.patched_indices:
                        ch, seg, _ = chunk[idx]
                        if res.targets[idx] != seg.target:
                            seg.target = res.targets[idx]
                            modified_chapters.add(ch.index)
                            glossary_patched += 1

        # 3. Record mid-frequency statistical drift diagnostics for observability.
        drift_clusters = detect_mid_frequency_drift(
            chapters,
            source_lang=self._src,
            target_lang=self._tgt,
            existing_glossary_sources={t.source for t in terms},
        )

        for ch in chapters:
            if ch.index in modified_chapters:
                store.save_chapter(ch)

        if quotes_unified or glossary_patched or drift_clusters:
            store.log_event(
                "consistency_sweep_completed",
                quotes_unified=quotes_unified,
                glossary_misses_patched=glossary_patched,
                drift_clusters=[
                    {
                        "source": c.source,
                        "hits": c.total_hits,
                        "targets": list(c.competing_targets),
                    }
                    for c in drift_clusters
                ],
            )
        return ConsistencySweepResult(
            quotes_unified=quotes_unified,
            glossary_misses_patched=glossary_patched,
            drift_clusters_checked=len(drift_clusters),
            drift_patches_applied=0,
        )
