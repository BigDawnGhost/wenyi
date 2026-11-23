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
from collections.abc import Mapping, Sequence
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
# High-frequency Chinese pronouns/particles/adverbs that never represent title/terminology drift.
_ZH_NOISE_CHARS = frozenset("我你他她它这這那了的着著过過吗嗎呢吧啊哦嗯就也都很再又")
_ZH_FUNCTION_BIGRAMS = frozenset(
    {
        "已经",
        "已經",
        "不是",
        "没有",
        "沒有",
        "什么",
        "什麼",
        "怎么",
        "怎麼",
        "可以",
        "因为",
        "因為",
        "所以",
        "但是",
        "如果",
        "虽然",
        "雖然",
        "而且",
        "或者",
        "自己",
        "时候",
        "時候",
        "只是",
        "不过",
        "不過",
        "还是",
        "還是",
        "然后",
        "然後",
        "知道",
        "觉得",
        "覺得",
        "看到",
        "看见",
        "看見",
        "说道",
        "說道",
        "问道",
        "問道",
        "答道",
        "笑道",
        "心中",
        "心里",
        "心裡",
        "如今",
        "原来",
        "原來",
        "实在",
        "實在",
        "其实",
        "其實",
        "自然",
        "当然",
        "當然",
        "应该",
        "應該",
        "必须",
        "必須",
        "一定",
        "一点",
        "一點",
        "一下",
        "一起",
        "一样",
        "一樣",
        "一直",
        "不能",
        "不会",
        "不會",
        "不知",
        "不敢",
        "不想",
        "不要",
        "不得",
        "无法",
        "無法",
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
    min_hits: int = 4,
    max_clusters: int = 12,
    extra_watch_sources: Mapping[str, Sequence[str]] | None = None,
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
    watch_map = extra_watch_sources or {}
    watched_norms = {normalize_match_text(s).strip() for s in watch_map}

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
                            or gram in _ZH_FUNCTION_BIGRAMS
                            or gram[0] in _CJK_EDGE_PARTICLES
                            or gram[-1] in _CJK_EDGE_PARTICLES
                            or any(c in _ZH_NOISE_CHARS for c in gram)
                            or all(c in _CJK_STOP_CHARS for c in gram)
                        ):
                            continue
                        norm = normalize_match_text(gram).strip()
                        if norm not in watched_norms and (
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
        is_watched = normalize_match_text(gram).strip() in watched_norms
        if len(occurrences) < (3 if is_watched else min_hits) or len(term_chapters[gram]) < 2:
            continue
        # Score distinctive target words that co-occur strongly with this source n-gram.
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
        min_lift = 1.15 if is_watched else 1.35
        for word, cnt in word_counts.most_common(12):
            share = cnt / n_occ
            base_rate = global_word_df[word] / total_paras
            if (
                cnt >= 2
                and 0.18 <= share <= 0.85
                and base_rate > 0
                and (share / base_rate) >= min_lift
            ):
                distinctive.append((word, cnt))

        if len(distinctive) >= 2:
            # Check mutual exclusivity of top two distinctive target words (split rendering signal!).
            (w1, c1), (w2, c2) = distinctive[0], distinctive[1]
            both = sum(
                1
                for _ci, _si, tgt in occurrences
                if re.search(rf"\b{re.escape(w1)}s?\b", tgt, re.I)
                and re.search(rf"\b{re.escape(w2)}s?\b", tgt, re.I)
            )
            if both <= min(c1, c2) * 0.30 and (c1 + c2) >= n_occ * 0.55:
                clusters.append(
                    DriftCluster(
                        source=gram,
                        total_hits=n_occ,
                        competing_targets=((w1, c1), (w2, c2)),
                        paragraph_refs=tuple((ci, si) for ci, si, _ in occurrences),
                    )
                )
                continue

        # For watched recurring titles, also catch mid-sentence capitalization splits
        # (e.g. "the King" in Ch 1 vs "the king" in Ch 2) that case-folding hides.
        if is_watched and word_counts:
            top_word, top_cnt = word_counts.most_common(1)[0]
            if top_cnt >= 3 and (top_cnt / n_occ) >= 0.40:
                cap_hits = 0
                low_hits = 0
                cap_chs: set[int] = set()
                low_chs: set[int] = set()
                pat = re.compile(rf"(?<=[a-z,;:—]\s)({re.escape(top_word)}s?)\b", re.I)
                for ci, _si, tgt in occurrences:
                    matches = pat.findall(tgt)
                    if not matches:
                        continue
                    has_cap = any(m[0].isupper() for m in matches)
                    has_low = any(m[0].islower() for m in matches)
                    if has_cap and not has_low:
                        cap_hits += 1
                        cap_chs.add(ci)
                    elif has_low and not has_cap:
                        low_hits += 1
                        low_chs.add(ci)
                if cap_hits >= 2 and low_hits >= 2 and len(cap_chs | low_chs) >= 2:
                    clusters.append(
                        DriftCluster(
                            source=gram,
                            total_hits=n_occ,
                            competing_targets=(
                                (top_word.capitalize(), cap_hits),
                                (top_word.lower(), low_hits),
                            ),
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
        conventions: Sequence[str] = (),
        divergent_titles: Mapping[str, Sequence[str]] | None = None,
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

        # 3. Detect mid-frequency title/term drift and unify confirmed splits surgically.
        drift_clusters = detect_mid_frequency_drift(
            chapters,
            source_lang=self._src,
            target_lang=self._tgt,
            existing_glossary_sources={t.source for t in terms},
            extra_watch_sources=divergent_titles,
        )
        drift_patched = 0
        drift_decisions: dict[str, str] = {}
        if drift_clusters:
            drift_decisions = self._editor.adjudicate_drift(
                [
                    {
                        "source": c.source,
                        "targets": list(c.competing_targets),
                    }
                    for c in drift_clusters
                ],
                style=style,
                conventions=conventions,
            )
        if drift_decisions:
            drift_items: list[tuple[Chapter, Segment, GlossaryTerm]] = []
            seen_drift_segs: set[int] = set()
            max_drift_paras = min(16, max_patch_paragraphs)
            for ch in chapters:
                prot = protected_by_ch[ch.index]
                for seg in ch.text_segments:
                    if id(seg) in prot or id(seg) in seen_drift_segs or not seg.target:
                        continue
                    for src_key, canonical_tgt in drift_decisions.items():
                        if not source_matches_text(src_key, seg.source):
                            continue
                        stripped_canon = canonical_tgt.strip()
                        core_canon = re.sub(r"^(?:the|a|an)\s+", "", stripped_canon, flags=re.I)
                        if self._tgt.lower().split("-", 1)[0] == "en" and core_canon:
                            if any(c.isupper() for c in core_canon):
                                # Respect intentional capitalization (e.g. "King" vs "king").
                                if re.search(rf"\b{re.escape(core_canon)}s?\b", seg.target):
                                    continue
                            else:
                                # Lowercase canonical (e.g. "king"): skip only if no mid-sentence
                                # capitalized variant remains and the canonical term is present.
                                cap_variant = core_canon[0].upper() + core_canon[1:]
                                has_mid_cap = bool(
                                    re.search(
                                        rf"(?<=[a-z,;:—]\s){re.escape(cap_variant)}s?\b",
                                        seg.target,
                                    )
                                )
                                if not has_mid_cap and _target_contains_term(
                                    seg.target, canonical_tgt, target_lang=self._tgt
                                ):
                                    continue
                        elif _target_contains_term(
                            seg.target, canonical_tgt, target_lang=self._tgt
                        ):
                            continue
                        drift_items.append(
                            (
                                ch,
                                seg,
                                GlossaryTerm(source=src_key, target=canonical_tgt, type="term"),
                            )
                        )
                        seen_drift_segs.add(id(seg))
                        break
                    if len(drift_items) >= max_drift_paras:
                        break
                if len(drift_items) >= max_drift_paras:
                    break
            for offset in range(0, len(drift_items), 8):
                chunk = drift_items[offset : offset + 8]
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
                            drift_patched += 1

        for ch in chapters:
            if ch.index in modified_chapters:
                store.save_chapter(ch)

        if quotes_unified or glossary_patched or drift_clusters or drift_patched:
            store.log_event(
                "consistency_sweep_completed",
                quotes_unified=quotes_unified,
                glossary_misses_patched=glossary_patched,
                drift_patches_applied=drift_patched,
                drift_decisions=drift_decisions,
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
            drift_patches_applied=drift_patched,
        )
