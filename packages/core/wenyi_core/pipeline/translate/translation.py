"""Translation batches, resume, polishing, rolling context, glossary extraction and titles.
Use the prepared book synopsis while the caller holds the book lock. Process chapters and
batches in order. For each batch: translate, persist targets, align/persist annotations,
update context, append the batch event, then extract glossary terms. During a run the
extraction model call overlaps the next batch's translation; its terms are stored, its
checkpoint written and history updated on this thread before the following extraction is
prepared, so the next batch's prompt lacks only the previous batch's new entries.
With ``pipeline.polish_concurrency`` above 1, standard mode saves each draft with a durable
polish-pending marker and hands polishing to a bounded background pool. Finished polish jobs
are applied on this thread in submission order; a chapter is published as done only after
its jobs are applied. Rolling context and glossary extraction may therefore read drafts.
At chapter end, perform fallback glossary extraction the same way and publish model text
plus done through save_chapter_with_status once its terms are stored. If targets exist
without a glossary checkpoint, extract missing terms without retranslating or overwriting
targets.
"""

from __future__ import annotations

import contextvars
import logging
import re
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ...agents.glossary_extractor import TranslatedSegmentEvidence
from ...agents.glossary_note import GlossaryNoteAgent
from ...config import PipelineConfig
from ...events import ProgressFn
from ...glossary.adherence import GlossaryAdherence
from ...glossary.prompt_scope import prompt_terms
from ...glossary.store import GlossaryStore
from ...ingest.tokens import count_tokens
from ...llm.retrying import is_resumable_provider_interrupt
from ...markup.contexts import annotation_contexts_for_segments, annotation_metadata
from ...models.document import Chapter, Segment
from ...models.glossary import GlossaryTerm
from ...storage.protocol import Storage
from ...storage.run_store import STATUS_DONE
from ..annotate.docx_styles import DocxStyleService
from .babeldoc_context import BabeldocContext
from .bible_seed import BibleSeedService
from .consistency_sweep import ConsistencySweepService
from .context import render_recent_translations
from .extraction_queue import ExtractionQueue
from .glossary_notes import GlossaryNotes
from .polish_queue import PolishOutcome, PolishQueue
from .post_normalize import (
    normalize_chapter_cjk_typography,
    normalize_chapter_latin_typography,
    normalize_minority_names,
)
from .precision import PrecisionBatchExecutor, PrecisionError
from .quote_memory import QuoteMemory
from .title_translation import TitleTranslationService
from .translation_batch import (
    BatchDraft,
    BatchPlan,
    BatchResult,
    PolishJob,
    TranslationBatchExecutor,
    keep_nonempty_drafts,
    resume_batches,
)

if TYPE_CHECKING:
    from ..annotate.annotations import AnnotationService
    from ..run.runtime import PipelineRuntime

_LOGGER = logging.getLogger(__name__)


# Segment meta marker saved with a draft whose polish has not been applied yet.
POLISH_PENDING = "polish_pending"
# Segment meta marker saved when polish failed after retries/splitting so resume can re-polish it.
POLISH_FAILED = "polish_failed"


def _draft_pairs(
    chapter: int, start_index: int, segments: list[Segment]
) -> list[tuple[tuple[int, int], str, str]]:
    """Return positioned source/draft pairs; drafts do not depend on when polish finishes."""
    return [
        (
            (chapter, start_index + offset),
            segment.source,
            segment.target_before_polish
            if segment.target_before_polish is not None
            else segment.target or "",
        )
        for offset, segment in enumerate(segments)
        if segment.target is not None
    ]


_LATIN_TARGET_LANGS = frozenset(
    {"en", "en-US", "en-GB", "fr", "de", "es", "it", "pt", "pt-BR", "pt-PT", "vi"}
)
_CJK_HYGIENE_TARGET_LANGS = frozenset({"zh", "zh-Hant"})
_HUGE_CHAPTER_BATCH_THRESHOLD = 12
_HUGE_CHAPTER_WAVE_SIZE = 6
_SOURCE_LOOKBEHIND_PARAS = 3


@dataclass(frozen=True)
class _WaveBatchSpec:
    batch_start: int
    segment_indices: tuple[int, ...]
    sources: tuple[str, ...]
    terms: tuple[GlossaryTerm, ...]
    annotation_contexts: list[list[dict[str, str]]]
    next_source: str
    already_done: bool
    existing_targets: tuple[str | None, ...]
    # False for PDF captions, headings and other non-body batches, which get no context.
    body_context: bool = True


@dataclass(frozen=True)
class _ParallelWaveSpec:
    chapter: int
    wave_index: int
    chapter_digest: str
    opening_source_lookbehind: str
    style: str
    book_synopsis: str
    batches: tuple[_WaveBatchSpec, ...]
    quote_references_by_batch: tuple[str, ...]
    polish: bool
    polish_mode: str
    entity_markers: bool
    rolling_context_tokens: int
    quote_memory: QuoteMemory | None = None
    variants_by_source: dict[str, tuple[str, ...]] | None = None


@dataclass(frozen=True)
class _WaveBatchOutcome:
    batch_start: int
    segment_indices: tuple[int, ...]
    sources: tuple[str, ...]
    result: BatchResult
    glossary_terms: int
    glossary_tokens: int


def _stops_run(error: BaseException) -> bool:
    """A provider interrupt, or a budget, deadline or cancel stop (``BaseException``)."""
    return not isinstance(error, Exception) or is_resumable_provider_interrupt(error)


def _set_hygienic_target(segment: Segment, cleaned: str) -> None:
    """Apply a hygiene fix; a target still equal to its draft keeps that equality, so an
    unpolished draft remains eligible when a resumed run repolishes it.
    """
    if cleaned == segment.target:
        return
    if segment.target_before_polish == segment.target:
        segment.target_before_polish = cleaned
    segment.target = cleaned


def _format_source_lookbehind(prev_digest: str, prev_sources: list[str]) -> str:
    """Build deterministic positional cross-chapter context from immutable source text."""
    parts: list[str] = []
    if prev_digest.strip():
        parts.append(f"[Preceding chapter digest (reference only)]\n{prev_digest.strip()}")
    cleaned = [s.strip() for s in prev_sources if s and s.strip()]
    if cleaned:
        joined = "\n".join(cleaned[-_SOURCE_LOOKBEHIND_PARAS:])
        parts.append(
            "[Preceding chapter closing source (reference only; do not translate)]\n" + joined
        )
    return "\n\n".join(parts)


class TranslationService:
    """Domain service for body translation, glossary extraction and chapter/TOC titles."""

    def __init__(self, runtime: PipelineRuntime, annotations: AnnotationService):
        self._runtime = runtime
        self._annotations = annotations
        self._docx_styles = DocxStyleService(runtime)
        self._titles = TitleTranslationService(runtime.title_translator)
        self._batches = TranslationBatchExecutor(
            runtime.translator, runtime.polisher, runtime.sparse_editor
        )
        self._bible_seed = BibleSeedService(runtime.book_bible, runtime.config)
        self._precision = PrecisionBatchExecutor(runtime.client, runtime.config)
        self._polish_queue: PolishQueue | None = None
        self._extraction_queue: ExtractionQueue | None = None
        # In-memory chapters this run still writes, including those awaiting polish results.
        self._open_chapters: dict[int, Chapter] = {}
        self._awaiting_polish: set[int] = set()
        # Hygiene terms fixed when a chapter's text and terms are final, used if it publishes
        # later once its polish lands, so job timing never changes the published text.
        self._publish_terms: dict[int, tuple[list[GlossaryTerm] | None, dict[str, list[str]]]] = {}
        # Extracted mappings the translator keeps overriding are left out of later prompts.
        self._adherence = GlossaryAdherence()
        self._left_out: frozenset[str] = frozenset()
        self._bible_variants: dict[str, tuple[str, ...]] = {}
        self._quote_memory: QuoteMemory | None = None

    def run(
        self,
        store: Storage,
        *,
        book_synopsis: str,
        only_chapter: int | None = None,
        progress: ProgressFn | None = None,
    ) -> Storage:
        """Translate chapters serially and persist usage/progress under the book lock.
        The caller restores languages, validates only_chapter and prepares the synopsis.
        This method performs body and title translation with restored context.
        """
        self._runtime.require_glossary_note_access()
        PipelineConfig.model_validate(self._runtime.config.pipeline.model_dump())
        manifest = store.load_manifest()
        glossary = store
        style = self._runtime.analyzer.style_brief(store.load_analysis() or {})

        pipeline = self._runtime.config.pipeline
        if only_chapter is not None:
            targets = [only_chapter]
            progress_chapters = targets
        else:
            targets = store.pending_chapters()
            if pipeline.polish and pipeline.translation_mode == "standard":
                unpolished = [
                    chapter["index"]
                    for chapter in manifest.get("chapters", [])
                    if chapter.get("unpolished") and chapter["index"] not in targets
                ]
                if unpolished:
                    order = [chapter["index"] for chapter in manifest.get("chapters", [])]
                    needed = set(targets) | set(unpolished)
                    targets = [idx for idx in order if idx in needed]
            progress_chapters = [chapter["index"] for chapter in manifest.get("chapters", [])]

        total, done = self.progress_counts(store, progress_chapters)
        drafts: list[tuple[tuple[int, int], str, str]] = []
        translation_history, source_corpus = self.load_translation_inputs(store, drafts)
        self._adherence = GlossaryAdherence()
        self._adherence.observe(drafts)
        self._left_out = frozenset()
        self._bible_variants = {}
        # Index the whole book, so a single-chapter run still sees quotes repeated elsewhere.
        chapter_sources_for_quotes = [
            (row["index"], [seg.source for seg in store.load_chapter(row["index"]).text_segments])
            for row in manifest.get("chapters", [])
        ]
        self._quote_memory = QuoteMemory.from_chapters(chapter_sources_for_quotes)
        for pos, src, tgt in drafts:
            self._quote_memory.observe(pos, src, tgt)

        if pipeline.glossary_mode == "compiled":
            # Compile from every chapter, even when this run translates only one, so the
            # Bible matches the whole-book source corpus it is mined against.
            digests = [
                str(store.load_chapter(row["index"]).meta.get("source_digest") or "")
                for row in manifest.get("chapters", [])
            ]
            repeated_quotes = (
                self._quote_memory.cross_chapter_spans() if self._quote_memory is not None else ()
            )
            self._bible_variants = self._bible_seed.ensure_seeded(
                store,
                digests=digests,
                source_corpus=source_corpus,
                book_synopsis=book_synopsis,
                style=style,
                repeated_quotes=repeated_quotes,
                progress=progress,
            )
            if self._bible_seed.last_conventions:
                conv_lines = "\n".join(f"- {c}" for c in self._bible_seed.last_conventions)
                conv_block = f"[Book-wide translation conventions]\n{conv_lines}"
                style = f"{style}\n\n{conv_block}".strip() if style else conv_block
            if self._bible_seed.last_voice_exemplars:
                ex_lines = "\n".join(
                    f"- SRC: {s}\n  TGT: {t}" for s, t in self._bible_seed.last_voice_exemplars
                )
                ex_block = (
                    "[Book voice exemplars — match this prose rhythm, dialogue cadence, and register]\n"
                    f"{ex_lines}"
                )
                style = f"{style}\n\n{ex_block}".strip() if style else ex_block
            if self._quote_memory is not None and self._bible_seed.last_quote_targets:
                for q_src, q_tgt in self._bible_seed.last_quote_targets.items():
                    self._quote_memory.seed_canonical(q_src, q_tgt)
            self._runtime.usage.flush(store, scope="bible")

        annotation_context_registry = store.load_annotation_contexts()
        store.log_event(
            "translate_run_started",
            only_chapter=only_chapter,
            chapters=targets,
            total_segments=total,
            glossary_mode=pipeline.glossary_mode,
            chapter_concurrency=pipeline.chapter_concurrency,
            polish_mode=pipeline.polish_mode,
        )
        if pipeline.chapter_concurrency > 1 and pipeline.glossary_mode != "compiled":
            # Parallel chapters plan every prompt up front, so terms extracted as earlier
            # chapters finish would never reach later ones; keep incremental mode serial.
            _LOGGER.warning(
                "pipeline.chapter_concurrency needs glossary_mode: compiled; "
                "translating chapters serially"
            )
        use_parallel_chapters = (
            pipeline.chapter_concurrency > 1
            and pipeline.glossary_mode == "compiled"
            and pipeline.translation_mode == "standard"
            and not pipeline.glossary_note_workers
            and bool(targets)
            and (
                len(targets) > 1
                or len(self._chapter_batches(store.load_chapter(targets[0])))
                > _HUGE_CHAPTER_BATCH_THRESHOLD
            )
        )
        if (
            not use_parallel_chapters
            and pipeline.polish
            and pipeline.translation_mode == "standard"
            and pipeline.polish_concurrency > 1
        ):
            polish_fn = (
                self._batches.polish
                if pipeline.polish_mode == "full"
                else (lambda job: self._batches.polish(job, polish_mode=pipeline.polish_mode))
            )
            self._polish_queue = PolishQueue(
                polish_fn,
                lambda outcome: self._apply_polish(store, outcome),
                workers=pipeline.polish_concurrency,
            )
        self._extraction_queue = ExtractionQueue()
        try:
            if use_parallel_chapters:
                done = self._run_parallel_chapters(
                    targets,
                    store,
                    glossary,
                    style,
                    book_synopsis,
                    translation_history=translation_history,
                    source_corpus=source_corpus,
                    annotation_context_registry=annotation_context_registry,
                    progress=progress,
                    done=done,
                    total=total,
                )
            else:
                for ci in targets:
                    done = self.translate_chapter(
                        ci,
                        store,
                        glossary,
                        style,
                        book_synopsis,
                        translation_history=translation_history,
                        source_corpus=source_corpus,
                        annotation_context_registry=annotation_context_registry,
                        progress=progress,
                        done=done,
                        total=total,
                    )
                    self._runtime.usage.flush(store, scope="chapter")
            # Store the last extraction, publishing its chapter, before polish drains.
            self._extraction_queue.settle()
            queue = self._polish_queue
            if queue is not None:
                if progress and total and len(queue):
                    progress(done, total, f"Polishing ({len(queue)} batches pending)")
                queue.drain()
            # Translate chapter/TOC titles after the body; keep the original book title and use glossary names.
            if not store.pending_chapters():
                if pipeline.consistency_sweep and only_chapter is None:
                    if progress:
                        progress(done, total, "Running cross-chapter consistency sweep…")
                    all_chapter_indices = [
                        chapter["index"] for chapter in manifest.get("chapters", [])
                    ]
                    sweep = ConsistencySweepService(
                        self._runtime.sparse_editor,
                        source_lang=self._runtime.config.source_lang,
                        target_lang=self._runtime.config.target_lang,
                    )
                    sweep.run(
                        store,
                        chapter_indices=all_chapter_indices,
                        terms=glossary.all_terms(),
                        style=style,
                        conventions=self._bible_seed.last_conventions,
                        divergent_titles=self._bible_seed.last_divergent_titles,
                    )
                    self._runtime.usage.flush(store, scope="consistency_sweep")
                self._titles.run(store, glossary, progress=progress)
                self._annotations.repair_fallbacks(store, progress=progress)
        except BaseException:
            # An unstored extraction has no checkpoint and is extracted again on resume.
            self._extraction_queue.close(cancel=True)
            if self._polish_queue is not None:
                # Unapplied jobs keep their durable marker and are repolished on resume.
                self._polish_queue.close(cancel=True)
            raise
        finally:
            self._extraction_queue.close()
            self._extraction_queue = None
            self._polish_queue = None
            self._open_chapters.clear()
            self._awaiting_polish.clear()
            self._publish_terms.clear()
            self._quote_memory = None
            self._runtime.usage.flush(store, scope="translate")
        if progress and total:
            progress(total, total, "progress.stage.translationDone")
        final_manifest = store.load_manifest()
        unpolished_chapters = [
            row["index"] for row in final_manifest.get("chapters", []) if row.get("unpolished")
        ]
        glossary_stats = store.stats()
        store.log_event(
            "translate_run_finished",
            total_segments=total,
            unpolished_chapters=unpolished_chapters,
            unpolished_chapter_count=len(unpolished_chapters),
            open_conflicts=glossary_stats.get("open_conflicts", 0),
        )
        return store

    @staticmethod
    def load_translation_inputs(
        store: Storage,
        drafts: list[tuple[tuple[int, int], str, str]] | None = None,
    ) -> tuple[dict[tuple[int, int], TranslatedSegmentEvidence], str]:
        """Read chapters once to rebuild translated-history indices and concatenate the source
        corpus. ``drafts``, when given, receives each translated paragraph's position, source
        and draft.
        """
        history: dict[tuple[int, int], TranslatedSegmentEvidence] = {}
        source_parts: list[str] = []
        manifest = store.load_manifest()
        chapter_indices = sorted(
            chapter["index"]
            for chapter in manifest.get("chapters", [])
            if isinstance(chapter.get("index"), int)
        )
        for chapter_index in chapter_indices:
            chapter = store.load_chapter(chapter_index)
            segments = chapter.text_segments
            source_parts.extend(segment.source for segment in segments)
            TranslationService.update_translation_history(history, chapter_index, 0, segments)
            if drafts is not None:
                drafts.extend(_draft_pairs(chapter_index, 0, segments))
        return history, "\n".join(source_parts)

    @staticmethod
    def update_translation_history(
        history: dict[tuple[int, int], TranslatedSegmentEvidence],
        chapter: int,
        start_index: int,
        segments: list[Segment],
    ) -> None:
        """Update the in-memory location index with the latest source/target batch."""
        for offset, segment in enumerate(segments):
            target = (segment.target or "").strip()
            if not target:
                continue
            segment_index = start_index + offset
            history[(chapter, segment_index)] = TranslatedSegmentEvidence(
                chapter=chapter,
                segment=segment_index,
                source=segment.source,
                target=target,
            )

    def progress_counts(self, store: Storage, chapter_indices: list[int]) -> tuple[int, int]:
        """Compute progress from batch checkpoints, starting resume at completed translation
        counts.
        Count a batch as done only when every target is not None (blank ``""`` counts). Counting
        partial batches early would duplicate completion counts if the batch reruns.
        """
        total = 0
        done = 0
        for ci in chapter_indices:
            chapter = store.load_chapter(ci)
            total += len(chapter.text_segments)
            for batch in self._chapter_batches(chapter):
                if all(segment.target is not None for segment in batch):
                    done += len(batch)
        return total, done

    def _chapter_batches(self, chapter: Chapter) -> list[list[Segment]]:
        """Apply source-specific boundaries without changing original text positions."""
        segments = chapter.text_segments
        policy = BabeldocContext.for_chapter(chapter)
        groups = policy.groups(segments) if policy is not None else [segments]
        return [
            batch
            for group in groups
            for batch in resume_batches(group, self._runtime.config.segment.max_tokens_per_batch)
        ]

    @staticmethod
    def _glossary_checkpoint_covers(checkpoints: set[str], start: int, count: int) -> bool:
        """Reuse completed extraction when BabelDOC role boundaries split an older batch."""
        ranges = []
        for key in checkpoints:
            match = re.fullmatch(r"(\d+):([1-9]\d*)", key)
            if match is not None:
                saved_start, saved_count = map(int, match.groups())
                ranges.append((saved_start, saved_start + saved_count))
        cursor = start
        for saved_start, saved_end in sorted(ranges):
            if saved_start <= cursor < saved_end:
                cursor = saved_end
                if cursor >= start + count:
                    return True
        return False

    def translate_chapter(
        self,
        ci: int,
        store: Storage,
        glossary: Storage | GlossaryStore,
        style: str,
        book_synopsis: str = "",
        *,
        translation_history: dict[tuple[int, int], TranslatedSegmentEvidence],
        source_corpus: str,
        annotation_context_registry: dict[str, Any] | None,
        progress: ProgressFn | None = None,
        done: int = 0,
        total: int = 0,
        allow_empty_translations: bool = False,
    ) -> int:
        """Translate, polish, extract and persist one chapter; return the updated
        completed-paragraph count.
        """
        with store.state_lock():
            chapter = store.load_chapter(ci)
            self._precision.recover_publications(store, chapter)
        text_segs = chapter.text_segments
        if not text_segs:
            store.set_chapter_status(ci, STATUS_DONE)
            store.log_event("chapter_skipped", chapter=ci, reason="empty")
            return done
        self._open_chapters[ci] = chapter
        chapter_digest = chapter.meta.get("source_digest", "")
        context_policy = BabeldocContext.for_chapter(chapter)
        annotation_contexts = annotation_contexts_for_segments(
            text_segs,
            annotation_context_registry,
        )

        batches = self._chapter_batches(chapter)
        notes = (
            GlossaryNotes(
                GlossaryNoteAgent(self._runtime.client, self._runtime.config),
                store,
                translation_history,
            )
            if self._runtime.config.pipeline.glossary_note_workers
            else None
        )
        covered_notes: set[str] = set()
        label = self.chapter_progress_label(chapter.title, ci)
        # Preparation often ends with a parsing label, but resume may first restore glossary terms.
        # Refresh at chapter start so the whole model call is not incorrectly labeled as source parsing.
        if progress:
            progress(done, total, label)
        glossary_checkpoints = store.completed_batch_glossary_keys(ci)
        # Read the full glossary in stable insertion order at chapter start.
        # Refresh lazily only if the glossary may have changed and another batch needs translation.
        # Fully checkpointed skips neither extract nor refresh. Saved translations lacking extraction
        # still extract and mark the snapshot stale, preserving resume completeness without redundant reads.
        term_snapshot = self.prompt_snapshot(store, glossary.all_terms())
        term_snapshot_stale = False

        # Process batches serially, deriving context from the latest formal targets.
        # This preserves pronoun, term and voice continuity between batches within a chapter.
        # Skip saved complete batches on resume and reconstruct context without retranslating them.
        compiled_glossary = self._runtime.config.pipeline.glossary_mode == "compiled"
        seg_base = 0  # Chapter-local index of this batch's first paragraph, used to map local issue indices.
        for b in batches:
            batch_start = seg_base
            glossary_key = store.batch_glossary_key(batch_start, len(b))
            if all(s.target is not None for s in b):
                # Reuse a batch translated at this position/context, rebuild rolling context and skip it.
                # Only None means not yet translated.
                marked = any(s.meta.get(POLISH_PENDING) or s.meta.get(POLISH_FAILED) for s in b)
                polish_pending = marked and (
                    self._runtime.config.pipeline.polish
                    and self._runtime.config.pipeline.translation_mode == "standard"
                )
                if marked and not polish_pending:
                    # Polishing is now off; the saved draft is final.
                    for s in b:
                        s.meta.pop(POLISH_PENDING, None)
                        s.meta.pop(POLISH_FAILED, None)
                    if not any(seg.meta.get(POLISH_FAILED) for seg in chapter.text_segments):
                        chapter.meta.pop("unpolished", None)
                        with store.state_lock():
                            manifest_now = store.load_manifest()
                            for row in manifest_now.get("chapters", []):
                                if row.get("index") == ci and row.get("unpolished"):
                                    row["unpolished"] = False
                                    store.save_manifest(manifest_now)
                                    break
                    store.save_chapter(chapter)
                self._align_batch_context(store, chapter, text_segs, batch_start, len(b))
                if (
                    compiled_glossary
                    or glossary_key in glossary_checkpoints
                    or (
                        context_policy is not None
                        and self._glossary_checkpoint_covers(
                            glossary_checkpoints, batch_start, len(b)
                        )
                    )
                ):
                    # Store a pending extraction now, so a skip never widens the lag.
                    if self._extraction_queue is not None and self._extraction_queue.settle():
                        term_snapshot_stale = True
                    summary = {
                        "inserted": 0,
                        "conflict": 0,
                        "unchanged": 0,
                        "updated": 0,
                        "skipped": 1,
                    }
                else:
                    # Targets exist but extraction checkpoint is missing: extract and store terms now.
                    summary = self.extract_batch_glossary(
                        glossary,
                        store,
                        ci,
                        batch_start,
                        b,
                        translation_history,
                        source_corpus,
                        admission=notes,
                    )
                    glossary_checkpoints.add(glossary_key)
                    term_snapshot_stale = True
                if notes is not None:
                    covered_notes.update(notes.maintain(ci, batch_start, b))
                    term_snapshot_stale = True
                store.log_event(
                    "batch_skipped",
                    chapter=ci,
                    start_index=batch_start,
                    count=len(b),
                    reason="already_translated",
                    glossary_extraction=summary,
                    segments=[
                        {"index": seg_base + i, "source": s.source, "target": s.target}
                        for i, s in enumerate(b)
                    ],
                )
                if polish_pending:
                    # Repolish an interrupted draft from saved text; never retranslate it.
                    if term_snapshot_stale:
                        term_snapshot = self.prompt_snapshot(store, glossary.all_terms())
                        term_snapshot_stale = False
                    body_context = context_policy is None or context_policy.body[batch_start]
                    polish_next_source = (
                        self.following_source(store, chapter, batch_start + len(b), context_policy)
                        if body_context
                        else ""
                    )
                    self._schedule_polish(
                        store,
                        PolishJob(
                            ci,
                            batch_start,
                            tuple(s.index for s in b),
                            tuple(s.source for s in b),
                            tuple(
                                deepcopy(
                                    self.batch_terms(
                                        term_snapshot,
                                        b,
                                        chapter_digest=chapter_digest if body_context else "",
                                        annotation_contexts=annotation_contexts[
                                            batch_start : batch_start + len(b)
                                        ],
                                        next_source=polish_next_source,
                                    )
                                )
                            ),
                            style,
                            polish_next_source,
                            # A target edited since the draft no longer matches and is kept.
                            BatchDraft(
                                tuple(
                                    s.target_before_polish
                                    if s.target_before_polish is not None
                                    else s.target or ""
                                    for s in b
                                )
                            ),
                        ),
                    )
                seg_base += len(b)
                if progress:
                    progress(done, total, label)
                continue

            if term_snapshot_stale:
                term_snapshot = self.prompt_snapshot(store, glossary.all_terms())
                term_snapshot_stale = False

            polish_job: PolishJob | None = None
            next_index = batch_start + len(b)
            body_context = context_policy is None or context_policy.body[batch_start]
            ctx_text = (
                self.recent_context(store, chapter, batch_start, context_policy)
                if body_context
                else ""
            )
            if body_context and self._quote_memory is not None:
                quote_ref = self._quote_memory.format_prompt_block([s.source for s in b])
                if quote_ref:
                    ctx_text = f"{ctx_text}\n\n{quote_ref}".strip() if ctx_text else quote_ref
            next_source = (
                self.following_source(store, chapter, next_index, context_policy)
                if body_context
                else ""
            )
            batch_digest = chapter_digest if body_context else ""
            batch_annotations = annotation_contexts[batch_start : batch_start + len(b)]
            plan = BatchPlan.capture(
                ci,
                batch_start,
                b,
                self.batch_terms(
                    term_snapshot,
                    b,
                    chapter_digest=batch_digest,
                    annotation_contexts=batch_annotations,
                    next_source=next_source,
                ),
                ctx_text,
                style,
                book_synopsis if body_context else "",
                batch_digest,
                batch_annotations,
                next_source,
                allow_empty_translations=allow_empty_translations,
                entity_markers=self._runtime.config.pipeline.entity_markers,
            )
            if self._runtime.config.pipeline.translation_mode == "three_draft_synthesis":

                def checkpoint_precision_usage() -> None:
                    self._runtime.usage.flush(store, scope="precision")

                result = self._precision.execute(
                    plan,
                    store,
                    checkpoint=checkpoint_precision_usage,
                    progress=(
                        (lambda phase: progress(done, total, f"{label} · {phase}"))
                        if progress
                        else None
                    ),
                )
                chapter = self.save_precision_batch(store, plan, result)
                # Precision publication reloads the chapter; alignment must use its segments.
                text_segs = chapter.text_segments
                b = text_segs[batch_start : batch_start + len(b)]
                self._precision.mark_published(store, result)
            elif self._polish_queue is not None:
                draft = self._batches.translate(plan)
                for segment, target in zip(b, draft.targets):
                    segment.target = target
                    segment.target_before_polish = target
                    segment.meta[POLISH_PENDING] = True
                # The draft and its pending marker are saved atomically with the chapter.
                store.save_chapter(chapter)
                polish_job = PolishJob.from_plan(plan, draft)
            else:
                result = self._batches.execute(
                    plan,
                    polish=self._runtime.config.pipeline.polish,
                    polish_mode=self._runtime.config.pipeline.polish_mode,
                )
                for segment, target, before_polish in zip(b, result.targets, result.before_polish):
                    segment.target = target
                    segment.target_before_polish = before_polish
                    if result.polish_failed:
                        segment.meta[POLISH_FAILED] = True
                    else:
                        segment.meta.pop(POLISH_FAILED, None)
                # Persist translations incrementally so interruption resumes after this batch.
                store.save_chapter(chapter)
                if result.polish_empty_reverted:
                    _LOGGER.warning(
                        "Polish returned empty output for %d non-empty segment(s) in chapter %d "
                        "batch %d; kept the translation draft.",
                        len(result.polish_empty_reverted),
                        ci,
                        batch_start,
                    )
                    store.log_event(
                        "batch_polish_empty_kept",
                        chapter=ci,
                        start_index=batch_start,
                        count=len(b),
                        reverted_indices=list(result.polish_empty_reverted),
                    )
                if result.polish_interrupted is not None:
                    self._runtime.usage.flush(store, scope="translation")
                    store.log_event(
                        "batch_polish_failed",
                        chapter=ci,
                        start_index=batch_start,
                        count=len(b),
                        reason="provider_interrupted",
                    )
                    raise result.polish_interrupted
                if result.polish_failed:
                    store.log_event(
                        "batch_polish_failed",
                        chapter=ci,
                        start_index=batch_start,
                        count=len(b),
                        reason="misaligned_output",
                    )
            # Align drafts now so later context never depends on polish timing; applying a
            # polish aligns the polished text again.
            self._align_batch_context(store, chapter, text_segs, batch_start, len(b))
            store.log_event(
                "batch_translated",
                chapter=ci,
                start_index=batch_start,
                count=len(b),
                polished=(
                    self._runtime.config.pipeline.polish
                    and polish_job is None
                    and not (
                        self._runtime.config.pipeline.translation_mode == "standard"
                        and result.polish_failed
                    )
                ),
                **({"polish_deferred": True} if polish_job is not None else {}),
                translation_mode=self._runtime.config.pipeline.translation_mode,
                glossary_terms=len(plan.terms),
                glossary_tokens=count_tokens(
                    self._runtime.translator.render_glossary_block(
                        list(plan.terms), list(plan.sources)
                    )
                ),
                segments=[
                    {
                        "index": batch_start + i,
                        "source": s.source,
                        "target": s.target,
                    }
                    for i, s in enumerate(b)
                ],
            )
            done += len(b)
            seg_base += len(b)
            if progress:
                progress(done, total, label)
            self._adherence.observe(_draft_pairs(ci, batch_start, b))

            def batch_extracted(
                _summary: dict[str, int],
                start: int = batch_start,
                batch: list[Segment] = b,
                key: str = glossary_key,
            ) -> None:
                if notes is not None:
                    covered_notes.update(notes.maintain(ci, start, batch))
                self.update_translation_history(translation_history, ci, start, batch)
                glossary_checkpoints.add(key)

            # Persist targets before glossary extraction so interruption cannot leave terms ahead of text.
            if not compiled_glossary:
                # During a run the model call overlaps the next batch's translation.
                self.extract_batch_glossary(
                    glossary,
                    store,
                    ci,
                    batch_start,
                    b,
                    translation_history,
                    source_corpus,
                    admission=notes,
                    finish=batch_extracted,
                )
                # The glossary may have changed; refresh before the next real translation, not after the final batch.
                term_snapshot_stale = True
            else:
                self.update_translation_history(translation_history, ci, batch_start, b)
            if self._quote_memory is not None:
                for offset, seg in enumerate(b):
                    if seg.target:
                        self._quote_memory.observe(
                            (ci, batch_start + offset),
                            seg.source,
                            seg.target,
                            prefer_update=True,
                        )
            if polish_job is not None:
                # Queue after extraction so terms come from the draft, not a later polish.
                self._schedule_polish(store, polish_job)

        # Precision publication reloads the chapter; publish the object this loop last saved.
        self._open_chapters[ci] = chapter

        def publish_when_polished(terms: list[GlossaryTerm] | None) -> None:
            # The chapter is done only once its terms are stored and its polish is applied.
            if self._polish_queue is not None and ci in self._polish_queue.pending_chapters():
                self._awaiting_polish.add(ci)
                self._publish_terms[ci] = (terms, self._hygiene_variants(store))
            else:
                self._publish_chapter(store, ci, terms=terms)

        if compiled_glossary:
            # The frozen Book Bible replaces per-chapter fallback extraction.
            publish_when_polished(term_snapshot)
            return done
        # Keep chapter-wide extraction as a fallback for address, speech and fixed expressions needing context.
        # Final review reads the stable glossary after the entire book finishes translating.
        src_text = "\n".join(s.source for s in text_segs)
        # With background polishing, read drafts so the result does not depend on job timing.
        tgt_text = "\n".join(
            (s.target_before_polish if self._polish_queue is not None else None) or s.target or ""
            for s in text_segs
        )
        chapter_segments = text_segs

        def chapter_extracted(summary: dict[str, int]) -> None:
            if notes is not None:
                notes.maintain(ci, 0, chapter_segments, exclude=covered_notes)
            store.log_event("chapter_glossary_extracted", chapter=ci, summary=summary)
            # Read the glossary once its last extraction for this chapter is stored.
            publish_when_polished(glossary.all_terms())

        self._extract(
            glossary,
            src_text,
            tgt_text,
            ci,
            history=translation_history,
            before=(ci, len(text_segs)),
            source_corpus=source_corpus,
            admission=notes,
            finish=chapter_extracted,
            overlap=True,
        )
        return done

    def _hygiene_variants(self, store: Storage) -> dict[str, list[str]]:
        """Known alternative spellings: Bible probe renderings plus open conflict proposals."""
        variants_by_source: dict[str, list[str]] = {
            k: list(v) for k, v in self._bible_variants.items()
        }
        try:
            for conflict in store.open_conflicts():
                src = conflict.get("source")
                prop = conflict.get("proposed_target")
                if isinstance(src, str) and isinstance(prop, str) and prop.strip():
                    variants_by_source.setdefault(src, []).append(prop.strip())
        except Exception:  # noqa: BLE001 - optional conflict enrichment
            pass
        return variants_by_source

    @staticmethod
    def _has_offset_bound_metadata(head: Segment) -> bool:
        """Return True when a logical paragraph stores offset-bound alignment placements."""
        ann = annotation_metadata(head)
        if isinstance(ann, dict) and isinstance(ann.get("items"), list) and ann["items"]:
            return True
        docx_styles = head.meta.get("docx_styles")
        return bool(
            not head.meta.get("docx_style")
            and isinstance(docx_styles, dict)
            and isinstance(docx_styles.get("items"), list)
            and docx_styles["items"]
        )

    def _apply_chapter_hygiene(
        self,
        store: Storage,
        chapter: Chapter,
        *,
        terms: list[GlossaryTerm] | None = None,
        variants: dict[str, list[str]] | None = None,
    ) -> None:
        """Apply zero-cost deterministic typography hygiene and minority-name normalization."""
        if not self._runtime.config.pipeline.deterministic_hygiene:
            return
        # Segments belonging to logical paragraphs with offset-bound annotation, protected-object,
        # or DOCX run-style placements must not be mutated after alignment without invalidating
        # their target_digest and character offsets.
        protected_ids: set[int] = set()
        current_head: Segment | None = None
        for seg in chapter.text_segments:
            if not seg.cont or current_head is None:
                current_head = seg
            if self._has_offset_bound_metadata(current_head):
                protected_ids.add(id(seg))
        text_segs = [s for s in chapter.text_segments if s.target]
        if not text_segs:
            return
        typo_fixes = 0
        target_lang = self._runtime.config.target_lang
        if target_lang in _LATIN_TARGET_LANGS:
            ch_pairs = [(seg.source, seg.target or "") for seg in text_segs]
            cleaned_targets, _ = normalize_chapter_latin_typography(
                ch_pairs, target_lang=target_lang
            )
            for seg, cleaned in zip(text_segs, cleaned_targets):
                if id(seg) in protected_ids:
                    continue
                if cleaned != seg.target:
                    _set_hygienic_target(seg, cleaned)
                    typo_fixes += 1
        elif target_lang in _CJK_HYGIENE_TARGET_LANGS:
            ch_pairs = [(seg.source, seg.target or "") for seg in text_segs]
            cleaned_targets, _ = normalize_chapter_cjk_typography(ch_pairs)
            for seg, cleaned in zip(text_segs, cleaned_targets):
                if id(seg) in protected_ids:
                    continue
                if cleaned != seg.target:
                    _set_hygienic_target(seg, cleaned)
                    typo_fixes += 1

        effective_terms = list(terms) if terms is not None else store.all_terms()
        variants_by_source = variants if variants is not None else self._hygiene_variants(store)

        pairs = [(seg.source, seg.target or "") for seg in text_segs]
        normalized_targets, raw_name_subs = normalize_minority_names(
            pairs,
            effective_terms,
            variants_by_source=variants_by_source,
            target_lang=target_lang,
        )
        for seg, updated in zip(text_segs, normalized_targets):
            if id(seg) in protected_ids:
                continue
            _set_hygienic_target(seg, updated)
        name_subs = [
            sub for sub in raw_name_subs if id(text_segs[sub["index"]]) not in protected_ids
        ]

        if typo_fixes or name_subs:
            store.log_event(
                "chapter_post_normalized",
                chapter=chapter.index,
                typography_fixes=typo_fixes,
                name_substitutions=len(name_subs),
                substitutions=name_subs[:50],
            )

    def _translate_wave_worker(
        self,
        spec: _ParallelWaveSpec,
        polish_executor: ThreadPoolExecutor | None = None,
        stop: threading.Event | None = None,
    ) -> tuple[int, int, list[_WaveBatchOutcome], BaseException | None, bool]:
        """Execute one chapter/wave's batches serially in a worker thread (model calls only).

        Never reads or writes Storage, SQLite, adherence or usage ledgers. Rolling context
        within the wave comes strictly from this wave's own drafts, while opening context is
        the deterministic positional source lookbehind captured before launch.
        When ``polish_executor`` is supplied and polishing is enabled, polish/sparse-edit
        calls overlap concurrently with subsequent batch translations inside the wave.
        A failed batch or stop signal ends the wave early; drafted batches are still returned,
        with ``completed=False``, so the writer saves them without publishing an incomplete chapter.
        """
        pending_outcomes: list[
            tuple[
                _WaveBatchSpec,
                BatchDraft,
                Any,
                BatchResult | None,
                int,
                int,
            ]
        ] = []
        wave_drafts: list[str] = []
        wave_qm = spec.quote_memory.clone() if spec.quote_memory is not None else None
        error: BaseException | None = None
        stopped_early = False

        def polish_interrupt() -> BaseException | None:
            """Return a finished polish job's provider interrupt, which stops the run."""
            for _, _, fut, _, _, _ in pending_outcomes:
                if fut is not None and fut.done() and not fut.cancelled():
                    exc = fut.exception()
                    if exc is not None and _stops_run(exc):
                        return exc
            return None

        try:
            for batch_idx, bspec in enumerate(spec.batches):
                if not bspec.already_done and (
                    (stop is not None and stop.is_set()) or polish_interrupt() is not None
                ):
                    # Spend nothing more on a wave whose run is stopping.
                    stopped_early = True
                    break
                if bspec.already_done:
                    if bspec.body_context:
                        wave_drafts.extend(t or "" for t in bspec.existing_targets)
                        if wave_qm is not None:
                            for offset, (src, tgt) in enumerate(
                                zip(bspec.sources, bspec.existing_targets)
                            ):
                                if tgt:
                                    wave_qm.observe(
                                        (spec.chapter, bspec.batch_start + offset), src, tgt
                                    )
                    continue
                if not bspec.body_context:
                    # Like the serial path, isolated material gets no rolling context.
                    ctx_text = ""
                elif wave_drafts:
                    ctx_text = render_recent_translations(
                        reversed(wave_drafts),
                        spec.rolling_context_tokens,
                    )
                else:
                    ctx_text = spec.opening_source_lookbehind
                if bspec.body_context and wave_qm is not None:
                    quote_ref = wave_qm.format_prompt_block(bspec.sources)
                else:
                    quote_ref = spec.quote_references_by_batch[batch_idx]
                if quote_ref:
                    ctx_text = f"{ctx_text}\n\n{quote_ref}".strip() if ctx_text else quote_ref

                plan = BatchPlan(
                    chapter=spec.chapter,
                    start_index=bspec.batch_start,
                    segment_indices=bspec.segment_indices,
                    sources=bspec.sources,
                    terms=bspec.terms,
                    context=ctx_text,
                    style=spec.style,
                    book_synopsis=spec.book_synopsis if bspec.body_context else "",
                    chapter_digest=spec.chapter_digest if bspec.body_context else "",
                    annotation_contexts=bspec.annotation_contexts,
                    next_source=bspec.next_source,
                    allow_empty_translations=False,
                    entity_markers=spec.entity_markers,
                    variants_by_source=spec.variants_by_source,
                )
                glossary_tokens = count_tokens(
                    self._runtime.translator.render_glossary_block(
                        list(plan.terms), list(plan.sources)
                    )
                )
                if spec.polish and polish_executor is not None:
                    draft = self._batches.translate(plan)
                    if bspec.body_context:
                        wave_drafts.extend(draft.targets)
                        if wave_qm is not None:
                            for offset, (src, tgt) in enumerate(zip(bspec.sources, draft.targets)):
                                wave_qm.observe(
                                    (spec.chapter, bspec.batch_start + offset), src, tgt
                                )
                    job = PolishJob.from_plan(plan, draft)
                    ctx_copy = contextvars.copy_context()
                    fut = polish_executor.submit(
                        ctx_copy.run,
                        self._batches.polish,
                        job,
                        polish_mode=spec.polish_mode,
                    )
                    pending_outcomes.append(
                        (bspec, draft, fut, None, len(plan.terms), glossary_tokens)
                    )
                else:
                    result = self._batches.execute(
                        plan,
                        polish=spec.polish,
                        polish_mode=spec.polish_mode,
                    )
                    draft_texts = [
                        bp if bp is not None else tgt
                        for tgt, bp in zip(result.targets, result.before_polish)
                    ]
                    if bspec.body_context:
                        wave_drafts.extend(draft_texts)
                        if wave_qm is not None:
                            for offset, (src, tgt) in enumerate(zip(bspec.sources, result.targets)):
                                wave_qm.observe(
                                    (spec.chapter, bspec.batch_start + offset), src, tgt
                                )
                    pending_outcomes.append(
                        (
                            bspec,
                            BatchDraft(tuple(draft_texts)),
                            None,
                            result,
                            len(plan.terms),
                            glossary_tokens,
                        )
                    )
                    if result.polish_interrupted is not None:
                        error = result.polish_interrupted
                        break
        except BaseException as exc:  # noqa: BLE001 - returned so drafted batches are saved first.
            # Includes budget and cancel stops, which subclass BaseException.
            error = exc

        outcomes: list[_WaveBatchOutcome] = []
        for bspec, draft, fut, direct_result, glossary_terms_n, glossary_tokens in pending_outcomes:
            if direct_result is not None:
                res = direct_result
            else:
                targets = list(draft.targets)
                before_polish: tuple[str | None, ...] = tuple(targets)
                polish_failed = False
                polish_empty_reverted: tuple[int, ...] = ()
                polish_interrupted: BaseException | None = None
                try:
                    polished = fut.result() if fut is not None else None
                except BaseException as exc:  # noqa: BLE001 - a budget stop is a BaseException.
                    polish_failed = True
                    if _stops_run(exc):
                        polish_interrupted = exc
                else:
                    if polished is not None and len(polished) == len(targets):
                        targets, polish_empty_reverted = keep_nonempty_drafts(targets, polished)
                    else:
                        polish_failed = True
                res = BatchResult(
                    tuple(targets),
                    before_polish,
                    polish_failed=polish_failed,
                    polish_empty_reverted=polish_empty_reverted,
                    polish_interrupted=polish_interrupted,
                )
            if error is None and res.polish_interrupted is not None:
                # Report it so the writer starts no further waves.
                error = res.polish_interrupted
            outcomes.append(
                _WaveBatchOutcome(
                    batch_start=bspec.batch_start,
                    segment_indices=bspec.segment_indices,
                    sources=bspec.sources,
                    result=res,
                    glossary_terms=glossary_terms_n,
                    glossary_tokens=glossary_tokens,
                )
            )
        completed = error is None and not stopped_early
        return spec.chapter, spec.wave_index, outcomes, error, completed

    def _run_parallel_chapters(
        self,
        targets: list[int],
        store: Storage,
        glossary: Storage | GlossaryStore,
        style: str,
        book_synopsis: str,
        *,
        translation_history: dict[tuple[int, int], TranslatedSegmentEvidence],
        source_corpus: str,
        annotation_context_registry: dict[str, Any] | None,
        progress: ProgressFn | None,
        done: int,
        total: int,
    ) -> int:
        """Translate multiple chapters (or huge-chapter waves) concurrently with a single writer."""
        pipeline = self._runtime.config.pipeline
        term_snapshot = self.prompt_snapshot(store, glossary.all_terms())
        manifest = store.load_manifest()
        all_chapter_indices = sorted(
            row["index"]
            for row in manifest.get("chapters", [])
            if isinstance(row.get("index"), int)
        )
        # Preload chapter metadata & immutable sources on the main thread for positional lookbehind.
        loaded_all: dict[int, Chapter] = {ci: store.load_chapter(ci) for ci in all_chapter_indices}

        waves: list[_ParallelWaveSpec] = []
        chapter_wave_counts: dict[int, int] = {}
        chapter_objects: dict[int, Chapter] = {}
        # Already-saved batches whose alignment may have been interrupted; aligned before publish.
        align_existing_batches: dict[int, list[tuple[int, int]]] = {}
        # Saved drafts whose polish a previous run never applied; repolished before publish.
        repolish_specs: dict[int, list[tuple[int, int, tuple[GlossaryTerm, ...], str]]] = {}

        for ci in targets:
            chapter = loaded_all[ci]
            text_segs = chapter.text_segments
            if not text_segs:
                store.set_chapter_status(ci, STATUS_DONE)
                store.log_event("chapter_skipped", chapter=ci, reason="empty")
                continue
            chapter_objects[ci] = chapter
            self._open_chapters[ci] = chapter
            chapter_digest = str(chapter.meta.get("source_digest") or "")
            context_policy = BabeldocContext.for_chapter(chapter)
            annotation_contexts = annotation_contexts_for_segments(
                text_segs,
                annotation_context_registry,
            )
            batches = self._chapter_batches(chapter)

            # Positional cross-chapter source lookbehind from immutable ci-1 source text.
            prev_indices = [idx for idx in all_chapter_indices if idx < ci]
            prev_digest = ""
            prev_sources: list[str] = []
            if prev_indices:
                prev_ch = loaded_all[prev_indices[-1]]
                prev_digest = str(prev_ch.meta.get("source_digest") or "")
                prev_sources = [s.source for s in prev_ch.text_segments]
            chapter_opening_lookbehind = _format_source_lookbehind(prev_digest, prev_sources)

            # Build per-batch specs.
            batch_specs: list[_WaveBatchSpec] = []
            quote_refs: list[str] = []
            seg_base = 0
            for b in batches:
                batch_start = seg_base
                next_idx = batch_start + len(b)
                body_context = context_policy is None or context_policy.body[batch_start]
                next_source = (
                    self.following_source(store, chapter, next_idx, context_policy)
                    if body_context
                    else ""
                )
                batch_annotations = annotation_contexts[batch_start : batch_start + len(b)]
                terms_for_batch = tuple(
                    deepcopy(
                        self.batch_terms(
                            term_snapshot,
                            b,
                            chapter_digest=chapter_digest if body_context else "",
                            annotation_contexts=batch_annotations,
                            next_source=next_source,
                        )
                    )
                )
                already_done = all(s.target is not None for s in b)
                if already_done:
                    align_existing_batches.setdefault(ci, []).append((batch_start, len(b)))
                if already_done and any(
                    s.meta.get(POLISH_PENDING) or s.meta.get(POLISH_FAILED) for s in b
                ):
                    if pipeline.polish:
                        # Repolish the saved draft after alignment; never retranslate it.
                        repolish_specs.setdefault(ci, []).append(
                            (batch_start, len(b), terms_for_batch, next_source)
                        )
                    else:
                        # Polishing is now off; the saved draft is final.
                        for s in b:
                            s.meta.pop(POLISH_PENDING, None)
                            s.meta.pop(POLISH_FAILED, None)
                batch_specs.append(
                    _WaveBatchSpec(
                        batch_start=batch_start,
                        segment_indices=tuple(s.index for s in b),
                        sources=tuple(s.source for s in b),
                        terms=terms_for_batch,
                        annotation_contexts=deepcopy(batch_annotations),
                        next_source=next_source,
                        already_done=already_done,
                        existing_targets=tuple(
                            s.target_before_polish
                            if s.target_before_polish is not None
                            else s.target
                            for s in b
                        ),
                        body_context=body_context,
                    )
                )
                quote_refs.append(
                    self._quote_memory.format_prompt_block([s.source for s in b])
                    if (body_context and self._quote_memory is not None)
                    else ""
                )
                seg_base += len(b)

            # Split huge single chapters (>12 batches) into parallel waves along batch chunks.
            if len(batch_specs) > _HUGE_CHAPTER_BATCH_THRESHOLD:
                wave_idx = 0
                for offset in range(0, len(batch_specs), _HUGE_CHAPTER_WAVE_SIZE):
                    chunk = batch_specs[offset : offset + _HUGE_CHAPTER_WAVE_SIZE]
                    qchunk = quote_refs[offset : offset + _HUGE_CHAPTER_WAVE_SIZE]
                    if offset == 0:
                        lookbehind = chapter_opening_lookbehind
                    else:
                        prev_wave_sources = [
                            src for bs in batch_specs[:offset] for src in bs.sources
                        ]
                        lookbehind = _format_source_lookbehind(chapter_digest, prev_wave_sources)
                    waves.append(
                        _ParallelWaveSpec(
                            chapter=ci,
                            wave_index=wave_idx,
                            chapter_digest=chapter_digest,
                            opening_source_lookbehind=lookbehind,
                            style=style,
                            book_synopsis=book_synopsis,
                            batches=tuple(chunk),
                            quote_references_by_batch=tuple(qchunk),
                            polish=pipeline.polish,
                            polish_mode=pipeline.polish_mode,
                            entity_markers=pipeline.entity_markers,
                            rolling_context_tokens=pipeline.rolling_context_tokens,
                            quote_memory=self._quote_memory.clone()
                            if self._quote_memory is not None
                            else None,
                            variants_by_source=dict(self._bible_variants)
                            if self._bible_variants
                            else None,
                        )
                    )
                    wave_idx += 1
                chapter_wave_counts[ci] = wave_idx
            else:
                waves.append(
                    _ParallelWaveSpec(
                        chapter=ci,
                        wave_index=0,
                        chapter_digest=chapter_digest,
                        opening_source_lookbehind=chapter_opening_lookbehind,
                        style=style,
                        book_synopsis=book_synopsis,
                        batches=tuple(batch_specs),
                        quote_references_by_batch=tuple(quote_refs),
                        polish=pipeline.polish,
                        polish_mode=pipeline.polish_mode,
                        entity_markers=pipeline.entity_markers,
                        rolling_context_tokens=pipeline.rolling_context_tokens,
                        quote_memory=self._quote_memory.clone()
                        if self._quote_memory is not None
                        else None,
                        variants_by_source=dict(self._bible_variants)
                        if self._bible_variants
                        else None,
                    )
                )
                chapter_wave_counts[ci] = 1

        if not waves:
            return done

        # Run model calls concurrently; persist results on THIS single writer thread in book order.
        completed_by_key: dict[
            tuple[int, int], tuple[list[_WaveBatchOutcome], BaseException | None, bool]
        ] = {}
        ordered_keys = [(w.chapter, w.wave_index) for w in waves]
        next_flush_idx = 0
        chapters_remaining_waves = dict(chapter_wave_counts)
        # The first failure in book order; later chapters are then saved but never published.
        failure: BaseException | None = None
        # Tells running waves to translate no further batches once the run is failing.
        stop = threading.Event()

        def _flush_ready(*, final: bool = False) -> None:
            nonlocal next_flush_idx, done, failure
            while next_flush_idx < len(ordered_keys):
                key = ordered_keys[next_flush_idx]
                if key not in completed_by_key:
                    if not final:
                        break
                    # A wave cancelled after a failure; save what later waves drafted.
                    next_flush_idx += 1
                    continue
                ci, _wave_idx = key
                # Drop each batch once its targets are persisted, so an interrupt mid-wave
                # leaves only unsaved batches for the final flush and never retries alignment.
                outcomes, error, wave_complete = completed_by_key[key]
                if failure is None and error is not None:
                    failure = error
                chapter = chapter_objects[ci]
                text_segs = chapter.text_segments
                try:
                    while outcomes:
                        out = outcomes[0]
                        b = text_segs[out.batch_start : out.batch_start + len(out.sources)]
                        res = out.result
                        for seg, tgt, bp in zip(b, res.targets, res.before_polish):
                            seg.target = tgt
                            seg.target_before_polish = bp
                            if res.polish_failed:
                                seg.meta[POLISH_FAILED] = True
                            else:
                                seg.meta.pop(POLISH_FAILED, None)
                        store.save_chapter(chapter)
                        if res.polish_interrupted is not None:
                            self._runtime.usage.flush(store, scope="translation")
                            store.log_event(
                                "batch_polish_failed",
                                chapter=ci,
                                start_index=out.batch_start,
                                count=len(b),
                                reason="provider_interrupted",
                            )
                            # The saved draft keeps its marker; stop after saving the rest.
                            error = error or res.polish_interrupted
                            if failure is None:
                                failure = error
                        if not final and failure is None and error is None and not stop.is_set():
                            # Skip alignment during teardown so a persistent cancel/budget stop
                            # cannot drop remaining drafts; resume aligns already_done batches.
                            self._align_batch_context(
                                store, chapter, text_segs, out.batch_start, len(b)
                            )
                        store.log_event(
                            "batch_translated",
                            chapter=ci,
                            start_index=out.batch_start,
                            count=len(b),
                            polished=pipeline.polish and not res.polish_failed,
                            parallel_chapter=True,
                            translation_mode=pipeline.translation_mode,
                            glossary_terms=out.glossary_terms,
                            glossary_tokens=out.glossary_tokens,
                            segments=[
                                {
                                    "index": out.batch_start + i,
                                    "source": s.source,
                                    "target": s.target,
                                }
                                for i, s in enumerate(b)
                            ],
                        )
                        self.update_translation_history(translation_history, ci, out.batch_start, b)
                        draft_pairs = _draft_pairs(ci, out.batch_start, b)
                        self._adherence.observe(draft_pairs)
                        if self._quote_memory is not None:
                            for offset, seg in enumerate(b):
                                if seg.target:
                                    self._quote_memory.observe(
                                        (ci, out.batch_start + offset),
                                        seg.source,
                                        seg.target,
                                        prefer_update=True,
                                    )
                        done += len(b)
                        del outcomes[0]
                        completed_by_key[key] = (outcomes, error, wave_complete)
                        if progress:
                            progress(done, total, self.chapter_progress_label(chapter.title, ci))
                except Exception:  # noqa: BLE001 - best-effort teardown saves later chapters.
                    if failure is None:
                        raise
                    _LOGGER.exception("Could not save drafted batches for chapter %d", ci)
                    del completed_by_key[key]
                    next_flush_idx += 1
                    continue
                del completed_by_key[key]
                if failure is None and wave_complete and not stop.is_set():
                    chapters_remaining_waves[ci] -= 1
                    if chapters_remaining_waves[ci] == 0:
                        for batch_start, batch_len in align_existing_batches.pop(ci, ()):
                            self._align_batch_context(
                                store, chapter, text_segs, batch_start, batch_len
                            )
                        for (
                            batch_start,
                            batch_len,
                            terms_for_batch,
                            next_source,
                        ) in repolish_specs.pop(ci, ()):
                            b = text_segs[batch_start : batch_start + batch_len]
                            self._schedule_polish(
                                store,
                                PolishJob(
                                    ci,
                                    batch_start,
                                    tuple(s.index for s in b),
                                    tuple(s.source for s in b),
                                    terms_for_batch,
                                    style,
                                    next_source,
                                    BatchDraft(
                                        tuple(
                                            s.target_before_polish
                                            if s.target_before_polish is not None
                                            else s.target or ""
                                            for s in b
                                        )
                                    ),
                                ),
                            )
                        if all(s.target is not None for s in text_segs):
                            self._publish_chapter(store, ci, terms=term_snapshot)
                            self._runtime.usage.flush(store, scope="chapter")
                else:
                    self._runtime.usage.flush(store, scope="translation")
                next_flush_idx += 1

        max_workers = min(pipeline.chapter_concurrency, len(waves))
        polish_workers = max(2, pipeline.polish_concurrency, max_workers) if pipeline.polish else 0
        polish_pool = (
            ThreadPoolExecutor(max_workers=polish_workers, thread_name_prefix="wenyi-par-polish")
            if polish_workers > 0
            else None
        )
        collected: set[tuple[int, int]] = set()
        interrupted: BaseException | None = None
        try:
            with ThreadPoolExecutor(
                max_workers=max_workers, thread_name_prefix="wenyi-chapter"
            ) as executor:
                futures = {
                    executor.submit(
                        contextvars.copy_context().run,
                        self._translate_wave_worker,
                        wave,
                        polish_pool,
                        stop,
                    ): wave
                    for wave in waves
                }
                try:
                    for future in as_completed(futures):
                        if future.cancelled():
                            continue
                        ci, wave_idx, outcomes, error, wave_complete = future.result()
                        collected.add((ci, wave_idx))
                        completed_by_key[(ci, wave_idx)] = (outcomes, error, wave_complete)
                        _flush_ready()
                        if error is not None or failure is not None:
                            # Start no new waves; running ones stop and are saved below.
                            stop.set()
                            for pending in futures:
                                pending.cancel()
                except BaseException as exc:
                    # A writer error or Ctrl-C: start no new waves either.
                    interrupted = exc
                    stop.set()
                    for pending in futures:
                        pending.cancel()
            if interrupted is not None:
                # Waves that finished while the pool shut down are paid for; keep them.
                for future, wave in futures.items():
                    key = (wave.chapter, wave.wave_index)
                    if key in collected or not future.done() or future.cancelled():
                        continue
                    if future.exception() is None:
                        _, _, outcomes, error, wave_complete = future.result()
                        completed_by_key[key] = (outcomes, error, wave_complete)
                failure = failure or interrupted
                try:
                    _flush_ready(final=True)
                except Exception:  # noqa: BLE001 - report the original error.
                    _LOGGER.exception("Could not save drafted batches after a failure")
                # Preserve an earlier chapter's failure over a secondary writer Exception,
                # while still surfacing a run-stopping BaseException (e.g. Ctrl-C).
                raise (interrupted if not isinstance(interrupted, Exception) else failure)
        finally:
            if polish_pool is not None:
                polish_pool.shutdown(wait=True)

        _flush_ready(final=True)
        if failure is not None:
            raise failure
        return done

    def _publish_chapter(
        self,
        store: Storage,
        ci: int,
        *,
        terms: list[GlossaryTerm] | None = None,
        variants: dict[str, list[str]] | None = None,
    ) -> None:
        chapter = self._open_chapters.pop(ci)
        self._awaiting_polish.discard(ci)
        self._apply_chapter_hygiene(store, chapter, terms=terms, variants=variants)
        unpolished = any(segment.meta.get(POLISH_FAILED) for segment in chapter.text_segments)
        if unpolished:
            chapter.meta["unpolished"] = True
            # Persist the manifest retry marker before marking the chapter done so a crash
            # between the two writes never leaves an unpolished chapter silently skipped.
            with store.state_lock():
                manifest = store.load_manifest()
                for row in manifest.get("chapters", []):
                    if row.get("index") == ci and not row.get("unpolished"):
                        row["unpolished"] = True
                        store.save_manifest(manifest)
                        break
        else:
            chapter.meta.pop("unpolished", None)
        store.save_chapter_with_status(chapter, STATUS_DONE)
        if not unpolished:
            with store.state_lock():
                manifest = store.load_manifest()
                for row in manifest.get("chapters", []):
                    if row.get("index") == ci and row.get("unpolished"):
                        row.pop("unpolished", None)
                        store.save_manifest(manifest)
                        break
        store.log_event(
            "chapter_done",
            chapter=ci,
            title=chapter.title,
            segment_count=len(chapter.text_segments),
            **({"unpolished": True} if unpolished else {}),
        )

    def prompt_snapshot(self, store: Storage, terms: list[GlossaryTerm]) -> list[GlossaryTerm]:
        """Leave out extracted mappings that finished translations keep overriding.

        The glossary itself is unchanged; a ``glossary_terms_left_out`` event records each
        change to the set so the decision is visible in the run log.
        """
        kept, left_out = self._adherence.filter(terms)
        current = frozenset(left_out)
        if current != self._left_out:
            store.log_event(
                "glossary_terms_left_out",
                added=sorted(current - self._left_out),
                restored=sorted(self._left_out - current),
                total=len(current),
            )
            self._left_out = current
        return kept

    def batch_terms(
        self,
        snapshot: list[GlossaryTerm],
        batch: list[Segment],
        *,
        chapter_digest: str,
        annotation_contexts: list[list[dict[str, str]]],
        next_source: str,
    ) -> list[GlossaryTerm]:
        """Select the glossary entries one batch sends under ``pipeline.batch_glossary`` and
        ``pipeline.glossary_prompt_tokens``.

        ``all`` sends the full snapshot and ``relevant`` keeps only matching entries. An opt-in
        token cap makes ``all`` keep its append-stable leading run plus matching later entries
        and limits ``relevant`` to the matching entries that fit.
        """
        parts = [segment.source for segment in batch]
        parts.extend(item.get("source", "") for group in annotation_contexts for item in group)
        parts.extend((next_source, chapter_digest))
        match_text = "\n".join(part for part in parts if part)
        return prompt_terms(
            snapshot,
            match_text,
            budget=self._runtime.config.pipeline.glossary_prompt_tokens,
            mode=self._runtime.config.pipeline.batch_glossary,
        )

    def _schedule_polish(self, store: Storage, job: PolishJob) -> None:
        """Queue a pending polish, or polish inline when no background pool is active."""
        queue = self._polish_queue
        if queue is not None:
            queue.submit(job)
            store.log_event(
                "batch_polish_queued",
                chapter=job.chapter,
                start_index=job.start_index,
                count=len(job.sources),
                backlog=len(queue),
            )
            return
        try:
            polish_mode = self._runtime.config.pipeline.polish_mode
            outcome = PolishOutcome(job, self._batches.polish(job, polish_mode=polish_mode))
        except Exception as error:  # noqa: BLE001 - a failed polish keeps the draft.
            outcome = PolishOutcome(job, None, error)
        self._apply_polish(store, outcome)

    def _apply_polish(self, store: Storage, outcome: PolishOutcome) -> None:
        """Apply one finished polish job to its in-memory chapter, then align and save."""
        job = outcome.job
        chapter = self._open_chapters[job.chapter]
        segments = chapter.text_segments[job.start_index : job.start_index + len(job.sources)]
        if tuple(segment.index for segment in segments) != job.segment_indices:
            raise RuntimeError("A polish job no longer matches its chapter batch")
        changed = 0
        reverted_indices: list[int] = []
        raw_polished = outcome.polished
        if raw_polished is not None:
            cleaned, reverted = keep_nonempty_drafts(job.draft.targets, raw_polished)
            reverted_indices = list(reverted)
        else:
            cleaned = list(job.draft.targets)
        for segment, polished in zip(segments, cleaned):
            pending = segment.meta.pop(POLISH_PENDING, None)
            failed_before = segment.meta.pop(POLISH_FAILED, None)
            eligible = bool(pending or failed_before)
            if outcome.polished is None:
                if eligible:
                    segment.meta[POLISH_FAILED] = True
                continue
            # Only marked drafts change. Alignment updates target and target_before_polish
            # together, so equality means the target is still the (aligned) draft; keep any
            # target edited since then.
            if eligible and segment.target == segment.target_before_polish:
                segment.target = polished
                changed += 1
        store.save_chapter(chapter)
        self._align_batch_context(
            store, chapter, chapter.text_segments, job.start_index, len(job.sources)
        )
        self._runtime.usage.flush(store, scope="polish")
        if reverted_indices:
            _LOGGER.warning(
                "Polish returned empty output for %d non-empty segment(s) in chapter %d "
                "batch %d; kept the translation draft.",
                len(reverted_indices),
                job.chapter,
                job.start_index,
            )
            store.log_event(
                "batch_polish_empty_kept",
                chapter=job.chapter,
                start_index=job.start_index,
                count=len(job.sources),
                reverted_indices=reverted_indices,
            )
        if outcome.polished is None:
            store.log_event(
                "batch_polish_failed",
                chapter=job.chapter,
                start_index=job.start_index,
                count=len(job.sources),
                reason=(
                    f"{type(outcome.error).__name__}: {outcome.error}"
                    if outcome.error is not None
                    else "misaligned_output"
                ),
            )
        else:
            store.log_event(
                "batch_polished",
                chapter=job.chapter,
                start_index=job.start_index,
                count=len(job.sources),
                applied=changed,
                segments=[
                    {"index": job.start_index + i, "target": segment.target}
                    for i, segment in enumerate(segments)
                ],
            )
        queue = self._polish_queue
        if job.chapter in self._awaiting_polish and (
            queue is None or job.chapter not in queue.pending_chapters()
        ):
            terms, variants = self._publish_terms.pop(job.chapter, (None, None))
            self._publish_chapter(store, job.chapter, terms=terms, variants=variants)

    def _align_batch_context(
        self,
        store: Storage,
        chapter: Chapter,
        segments: list[Segment],
        start: int,
        count: int,
    ) -> None:
        """Align formal targets before subsequent batches derive their context.

        Alignment handles touched logical paragraphs in source order, waiting for their
        final continuation. It can also update targets from earlier batches.
        """
        self._annotations.align_annotations_after_batch(
            chapter.index, chapter, start, count, store, segments=segments
        )
        self._docx_styles.align_styles_after_batch(
            chapter.index, chapter, start, count, store, segments=segments
        )

    @staticmethod
    def save_precision_batch(store: Storage, plan: BatchPlan, result: BatchResult) -> Chapter:
        """Merge accepted pending segments into a fresh chapter without losing manual edits."""
        with store.state_lock():
            publication = PrecisionBatchExecutor.prepared_publication(store, result)
            if (
                publication.get("segment_indices") != list(plan.segment_indices)
                or len(result.targets) != len(plan.sources)
                or len(result.before_polish) != len(plan.sources)
            ):
                raise PrecisionError("The precision publication index is not aligned to its batch")
            chapter = store.load_chapter(plan.chapter)
            segments = chapter.text_segments[
                plan.start_index : plan.start_index + len(plan.sources)
            ]
            if (
                tuple(segment.index for segment in segments) != plan.segment_indices
                or tuple(segment.source for segment in segments) != plan.sources
            ):
                raise PrecisionError("The precision batch source changed before publication")
            for segment, target, before_polish in zip(
                segments, result.targets, result.before_polish
            ):
                if segment.target is not None and (
                    segment.target != target or segment.target_before_polish != before_polish
                ):
                    raise PrecisionError(
                        "The precision batch was edited before publication; saved candidates are retained"
                    )
            for segment, target, before_polish in zip(
                segments, result.targets, result.before_polish
            ):
                segment.target = target
                segment.target_before_polish = before_polish
            store.save_chapter(chapter)
            return chapter

    @staticmethod
    def chapter_progress_label(title: str, index: int) -> str:
        """Prefer the book's chapter title for progress so internal indices cannot contradict
        visible numbering.
        """
        title = (title or "").strip()
        return title or f"progress.stage.chapter:{index + 1}"

    def extract_batch_glossary(
        self,
        glossary: Storage | GlossaryStore,
        store: Storage,
        chapter: int,
        start_index: int,
        batch,
        translation_history: dict[tuple[int, int], TranslatedSegmentEvidence],
        source_corpus: str,
        *,
        admission: GlossaryNotes | None = None,
        finish: Callable[[dict[str, int]], None] | None = None,
    ) -> dict[str, int] | None:
        """Extract terms after translating or resuming a batch for use by later batches.

        Without ``finish``, store the terms now and return the summary. With ``finish``
        during a run, the model call overlaps the next batch's translation; the terms are
        stored and ``finish`` runs on this thread when that job settles, and None is
        returned.
        """
        src_text = "\n".join(s.source for s in batch)
        tgt_text = "\n".join(s.target or "" for s in batch)

        def stored(summary: dict[str, int]) -> None:
            # This event is the batch's extraction checkpoint.
            store.log_event(
                "batch_glossary_extracted",
                chapter=chapter,
                start_index=start_index,
                count=len(batch),
                summary=summary,
            )
            if finish is not None:
                finish(summary)

        return self._extract(
            glossary,
            src_text,
            tgt_text,
            chapter,
            history=translation_history,
            before=(chapter, start_index),
            source_corpus=source_corpus,
            admission=admission,
            finish=stored,
            overlap=finish is not None,
        )

    def _extract(
        self,
        glossary: Storage | GlossaryStore,
        source_text: str,
        target_text: str,
        chapter: int,
        *,
        history: dict[tuple[int, int], TranslatedSegmentEvidence],
        before: tuple[int, int],
        source_corpus: str,
        admission: GlossaryNotes | None,
        finish: Callable[[dict[str, int]], None],
        overlap: bool,
    ) -> dict[str, int] | None:
        """Store one extraction now, or overlap its model call with the next translation.

        Either way the pending job settles first, so extractions are stored in order.
        """
        extractor = self._runtime.extractor
        admit = {"admit": admission.admit} if admission is not None else {}
        queue = self._extraction_queue
        if queue is not None:
            queue.settle()
        if queue is None or not overlap:
            summary = extractor.extract_and_store(
                glossary,
                source_text,
                target_text,
                chapter,
                history=history.values(),
                before=before,
                source_corpus=source_corpus,
                **admit,
            )
            finish(summary)
            return summary
        request = extractor.prepare_extraction(
            glossary, source_text, target_text, source_corpus=source_corpus
        )

        def store_terms(terms: list[GlossaryTerm]) -> None:
            finish(
                extractor.store_extracted(
                    glossary,
                    request,
                    terms,
                    chapter,
                    history=history.values(),
                    before=before,
                    source_corpus=source_corpus,
                    **admit,
                )
            )

        queue.submit(lambda: extractor.extract_candidates(request), store_terms)
        return None

    def recent_context(
        self,
        store: Storage,
        chapter: Chapter,
        start: int,
        policy: BabeldocContext | None,
    ) -> str:
        """Read the translated prefix backward, loading preceding chapters only as needed.

        Formal chapters are the sole source of context on fresh and resumed runs. Chapter
        boundaries do not stop history; BabelDOC still filters layout noise and stops at headings.
        With background polishing, context reads drafts so it never depends on job timing.
        """

        def target(segment: Segment) -> str | None:
            if self._polish_queue is not None and segment.target_before_polish is not None:
                return segment.target_before_polish
            return segment.target

        def targets() -> Iterator[str | None]:
            if policy is not None:
                yield from reversed(policy.recent_targets(chapter.text_segments, start, target))
                if start and policy.sections[start - 1] != 0:
                    return
            else:
                yield from (target(segment) for segment in reversed(chapter.text_segments[:start]))
            for previous in self._neighboring_chapters(store, chapter.index, following=False):
                segments = previous.text_segments
                previous_policy = BabeldocContext.for_chapter(previous)
                if previous_policy is None:
                    yield from (target(segment) for segment in reversed(segments))
                else:
                    yield from reversed(
                        previous_policy.recent_targets(segments, len(segments), target)
                    )
                    if previous_policy.sections and previous_policy.sections[-1] != 0:
                        return

        return render_recent_translations(
            targets(), self._runtime.config.pipeline.rolling_context_tokens
        )

    def following_source(
        self,
        store: Storage,
        chapter: Chapter,
        end: int,
        policy: BabeldocContext | None,
    ) -> str:
        """Find one following source reference in book order without translating it early."""
        segments = chapter.text_segments
        if policy is None:
            if end < len(segments):
                return segments[end].source
        else:
            if end <= 0 or not policy.body[end - 1]:
                return ""
            reference = policy.next_source(segments, end)
            if reference:
                return reference
            if policy.sections[-1] != policy.sections[end - 1]:
                return ""
        for following in self._neighboring_chapters(store, chapter.index, following=True):
            segments = following.text_segments
            following_policy = BabeldocContext.for_chapter(following)
            if following_policy is None:
                if segments:
                    return segments[0].source
            else:
                reference = following_policy.next_source(segments, 0)
                if reference:
                    return reference
                if any(following_policy.sections):
                    return ""
        return ""

    @staticmethod
    def _neighboring_chapters(
        store: Storage, chapter: int, *, following: bool
    ) -> Iterator[Chapter]:
        """Load neighboring chapters lazily in stable source order."""
        indices = sorted(
            row["index"]
            for row in store.load_manifest().get("chapters", [])
            if isinstance(row.get("index"), int)
            and (row["index"] > chapter if following else row["index"] < chapter)
        )
        for index in indices if following else reversed(indices):
            yield store.load_chapter(index)
