"""Translation batches, resume, polishing, rolling context, glossary extraction and titles.
Use the prepared book synopsis while the caller holds the book lock. In serial mode,
process chapters and batches in order (overlapping one glossary extraction and optional
background polish under a single state writer). When ``chapter_concurrency > 1``, seed a
frozen upfront CJK glossary and delegate concurrent chapters to ``ParallelChapterRunner``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from ...agents.glossary_extractor import TranslatedSegmentEvidence
from ...agents.glossary_note import GlossaryNoteAgent
from ...config import PipelineConfig
from ...events import ProgressFn
from ...glossary.store import GlossaryStore
from ...llm.retrying import is_resumable_provider_interrupt
from ...markup.contexts import annotation_contexts_for_segments
from ...models.document import Chapter, Segment
from ...models.glossary import GlossaryTerm
from ...storage.protocol import Storage
from ...storage.run_store import STATUS_DONE
from ..annotate.docx_styles import DocxStyleService
from .babeldoc_context import BabeldocContext
from .context import following_chapter_source, recent_chapter_context
from .extraction_queue import ExtractionQueue
from .glossary_notes import GlossaryNotes
from .parallel_chapters import (
    ParallelChapterRunner,
    pending_or_unpolished_chapters,
    publish_chapter_status,
)
from .polish_queue import PolishOutcome, PolishQueue
from .post_normalize import repair_escaped_newlines
from .precision import PrecisionBatchExecutor, save_precision_batch
from .seed_service import CjkGlossarySeedService
from .title_translation import TitleTranslationService
from .translation_batch import (
    POLISH_ATTEMPTS,
    POLISH_FAILED,
    POLISH_PENDING,
    BatchDraft,
    BatchPlan,
    PolishJob,
    TranslationBatchExecutor,
    glossary_checkpoint_covers,
    keep_nonempty_drafts,
    mark_polish_failure,
    resume_batches,
)

if TYPE_CHECKING:
    from ..annotate.annotations import AnnotationService
    from ..run.runtime import PipelineRuntime

_LOGGER = logging.getLogger(__name__)


class TranslationService:
    """Domain service for body translation, glossary extraction and chapter/TOC titles."""

    def __init__(self, runtime: PipelineRuntime, annotations: AnnotationService):
        self._runtime = runtime
        self._annotations = annotations
        self._docx_styles = DocxStyleService(runtime)
        self._titles = TitleTranslationService(runtime.title_translator)
        self._batches = TranslationBatchExecutor(
            runtime.translator,
            runtime.polisher,
            runtime.sparse_editor,
        )
        self._precision = PrecisionBatchExecutor(runtime.client, runtime.config)
        self._seeder = CjkGlossarySeedService(runtime, runtime.cjk_seeder)
        self._parallel = ParallelChapterRunner(
            self._batches,
            chapter_batches_fn=self._chapter_batches,
            align_batch_fn=self._align_batch_context,
            progress_label_fn=self.chapter_progress_label,
            flush_usage_fn=lambda st, scope: self._runtime.usage.flush(st, scope=scope),
        )
        self._polish_queue: PolishQueue | None = None
        self._extraction_queue: ExtractionQueue | None = None
        self._open_chapters: dict[int, Chapter] = {}
        self._awaiting_polish: set[int] = set()

    def run(
        self,
        store: Storage,
        *,
        book_synopsis: str,
        only_chapter: int | None = None,
        progress: ProgressFn | None = None,
    ) -> Storage:
        """Translate chapters and persist usage/progress under the book lock."""
        self._runtime.require_glossary_note_access()
        pipeline = PipelineConfig.model_validate(self._runtime.config.pipeline.model_dump())
        manifest = store.load_manifest()
        glossary = store
        style = self._runtime.analyzer.style_brief(store.load_analysis() or {})

        if only_chapter is not None:
            targets = progress_chapters = [only_chapter]
        else:
            targets = pending_or_unpolished_chapters(
                store,
                manifest,
                include_unpolished=pipeline.polish and pipeline.translation_mode == "standard",
            )
            progress_chapters = [chapter["index"] for chapter in manifest.get("chapters", [])]

        total, done = self.progress_counts(store, progress_chapters)
        translation_history, source_corpus = self.load_translation_inputs(store)
        annotation_context_registry = store.load_annotation_contexts()

        use_parallel_chapters = (
            pipeline.chapter_concurrency > 1
            and pipeline.translation_mode == "standard"
            and not pipeline.glossary_note_workers
            and len(targets) > 1
        )
        if use_parallel_chapters:
            digests = [
                str(store.load_chapter(row["index"]).meta.get("source_digest") or "")
                for row in manifest.get("chapters", [])
            ]
            self._seeder.ensure_seeded(
                store,
                digests=digests,
                source_corpus=source_corpus,
                book_synopsis=book_synopsis,
                style=style,
                progress=progress,
                checkpoint_usage=lambda: self._runtime.usage.flush(store, scope="seed"),
            )
            self._runtime.usage.flush(store, scope="seed")

        store.log_event(
            "translate_run_started",
            only_chapter=only_chapter,
            chapters=targets,
            total_segments=total,
            chapter_concurrency=pipeline.chapter_concurrency,
            polish_mode=pipeline.polish_mode,
        )

        if (
            not use_parallel_chapters
            and pipeline.polish
            and pipeline.translation_mode == "standard"
            and pipeline.polish_concurrency > 1
        ):
            polish_fn: Callable[[PolishJob], tuple[str, ...] | None] = (
                (lambda job: self._batches.polish(job))
                if pipeline.polish_mode == "full"
                else (lambda job: self._batches.polish(job, polish_mode=pipeline.polish_mode))
            )
            self._polish_queue = PolishQueue(
                polish_fn,
                lambda outcome: self._apply_polish(store, outcome),
                workers=pipeline.polish_concurrency,
            )
        self._extraction_queue = ExtractionQueue()
        completed_cleanly = False
        try:
            if use_parallel_chapters:
                done = self._parallel.run(
                    targets,
                    store,
                    frozen_terms=glossary.all_terms(),
                    style=style,
                    book_synopsis=book_synopsis,
                    annotation_context_registry=annotation_context_registry,
                    chapter_concurrency=pipeline.chapter_concurrency,
                    rolling_context_tokens=pipeline.rolling_context_tokens,
                    polish=pipeline.polish,
                    polish_mode=pipeline.polish_mode,
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
                if self._extraction_queue is not None:
                    self._extraction_queue.settle()
                if self._polish_queue is not None:
                    self._polish_queue.drain()
            if not store.pending_chapters():
                self._titles.run(store, glossary, progress=progress)
                self._annotations.repair_fallbacks(store, progress=progress)
            completed_cleanly = True
        finally:
            if self._extraction_queue is not None:
                self._extraction_queue.close(cancel=not completed_cleanly)
                self._extraction_queue = None
            if self._polish_queue is not None:
                self._polish_queue.close(cancel=not completed_cleanly)
                self._polish_queue = None
            self._open_chapters.clear()
            self._awaiting_polish.clear()
            self._runtime.usage.flush(store, scope="translate")
        if progress and total:
            progress(total, total, "progress.stage.translationDone")
        store.log_event("translate_run_finished", total_segments=total)
        return store

    @staticmethod
    def load_translation_inputs(
        store: Storage,
    ) -> tuple[dict[tuple[int, int], TranslatedSegmentEvidence], str]:
        """Read chapters once to rebuild translated-history indices and concatenate source."""
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
        """Compute progress from batch checkpoints; blank ``""`` counts as translated."""
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

    _glossary_checkpoint_covers = staticmethod(glossary_checkpoint_covers)

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
        """Translate, polish, extract and persist one chapter serially."""
        with store.state_lock():
            chapter = store.load_chapter(ci)
            self._precision.recover_publications(store, chapter)
            manifest_row = next(
                (r for r in store.load_manifest().get("chapters", []) if r.get("index") == ci),
                {},
            )
            reopened_done = manifest_row.get("status") == STATUS_DONE
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

        pipeline = self._runtime.config.pipeline
        batches = self._chapter_batches(chapter)
        notes = (
            GlossaryNotes(
                GlossaryNoteAgent(self._runtime.client, self._runtime.config),
                store,
                translation_history,
            )
            if pipeline.glossary_note_workers
            else None
        )
        covered_notes: set[str] = set()
        label = self.chapter_progress_label(chapter.title, ci)
        if progress:
            progress(done, total, label)
        glossary_checkpoints = store.completed_batch_glossary_keys(ci)
        term_snapshot = glossary.all_terms()
        term_snapshot_stale = False

        seg_base = 0
        for b in batches:
            batch_start = seg_base
            glossary_key = store.batch_glossary_key(batch_start, len(b))
            if all(s.target is not None for s in b):
                polish_pending = pipeline.polish and any(
                    s.meta.get(POLISH_PENDING) or s.meta.get(POLISH_FAILED) for s in b
                )
                self._align_batch_context(store, chapter, text_segs, batch_start, len(b))
                skip_extract = (
                    reopened_done
                    or glossary_key in glossary_checkpoints
                    or (
                        context_policy is not None
                        and self._glossary_checkpoint_covers(
                            glossary_checkpoints, batch_start, len(b)
                        )
                    )
                )
                if skip_extract:
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
                    if term_snapshot_stale:
                        term_snapshot = glossary.all_terms()
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
                            tuple(deepcopy(term_snapshot)),
                            style,
                            polish_next_source,
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
                self._runtime.usage.flush(store, scope="batch")
                continue

            if self._polish_queue is not None:
                self._polish_queue.collect()
            if term_snapshot_stale:
                term_snapshot = glossary.all_terms()
                term_snapshot_stale = False

            polish_job: PolishJob | None = None
            next_index = batch_start + len(b)
            body_context = context_policy is None or context_policy.body[batch_start]
            ctx_text = (
                self.recent_context(store, chapter, batch_start, context_policy)
                if body_context
                else ""
            )
            next_source = (
                self.following_source(store, chapter, next_index, context_policy)
                if body_context
                else ""
            )
            plan = BatchPlan.capture(
                ci,
                batch_start,
                b,
                term_snapshot,
                ctx_text,
                style,
                book_synopsis if body_context else "",
                chapter_digest if body_context else "",
                annotation_contexts[batch_start : batch_start + len(b)],
                next_source,
                allow_empty_translations=allow_empty_translations,
            )
            if pipeline.translation_mode == "three_draft_synthesis":
                result = self._precision.execute(
                    plan,
                    store,
                    checkpoint=lambda: self._runtime.usage.flush(store, scope="precision"),
                    progress=(
                        (lambda phase: progress(done, total, f"{label} · {phase}"))
                        if progress
                        else None
                    ),
                )
                chapter = self.save_precision_batch(store, plan, result)
                self._open_chapters[ci] = chapter
                text_segs = chapter.text_segments
                b = text_segs[batch_start : batch_start + len(b)]
                self._precision.mark_published(store, result)
            elif self._polish_queue is not None:
                draft = self._batches.translate(plan)
                cleaned_draft = tuple(
                    repair_escaped_newlines(s.source, t) for s, t in zip(b, draft.targets)
                )
                draft = BatchDraft(cleaned_draft, draft.turn, draft.turn_indices)
                for segment, target in zip(b, draft.targets):
                    segment.target = segment.target_before_polish = target
                    segment.meta[POLISH_PENDING] = True
                    segment.meta.pop(POLISH_FAILED, None)
                store.save_chapter(chapter)
                polish_job = PolishJob.from_plan(plan, draft)
            else:
                result = self._batches.execute(
                    plan, polish=pipeline.polish, polish_mode=pipeline.polish_mode
                )
                for segment, target, before_polish in zip(b, result.targets, result.before_polish):
                    segment.target = repair_escaped_newlines(segment.source, target)
                    segment.target_before_polish = (
                        repair_escaped_newlines(segment.source, before_polish)
                        if before_polish is not None
                        else None
                    )
                    segment.meta.pop(POLISH_PENDING, None)
                    if result.polish_failed and pipeline.polish:
                        mark_polish_failure(
                            segment, interrupted=result.polish_interrupted is not None
                        )
                    else:
                        segment.meta.pop(POLISH_FAILED, None)
                        segment.meta.pop(POLISH_ATTEMPTS, None)
                store.save_chapter(chapter)
                if result.polish_empty_reverted:
                    store.log_event(
                        "batch_polish_empty_kept",
                        chapter=ci,
                        start_index=batch_start,
                        count=len(b),
                        reverted_indices=list(result.polish_empty_reverted),
                    )
                if result.polish_interrupted is not None:
                    self._align_batch_context(store, chapter, text_segs, batch_start, len(b))
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
            self._align_batch_context(store, chapter, text_segs, batch_start, len(b))
            store.log_event(
                "batch_translated",
                chapter=ci,
                start_index=batch_start,
                count=len(b),
                polished=(
                    pipeline.polish
                    and polish_job is None
                    and not (pipeline.translation_mode == "standard" and result.polish_failed)
                ),
                **({"polish_deferred": True} if polish_job is not None else {}),
                translation_mode=pipeline.translation_mode,
                segments=[
                    {"index": batch_start + i, "source": s.source, "target": s.target}
                    for i, s in enumerate(b)
                ],
            )
            done += len(b)
            seg_base += len(b)
            if progress:
                progress(done, total, label)

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
            term_snapshot_stale = True
            if polish_job is not None:
                self._schedule_polish(store, polish_job)
            self._runtime.usage.flush(store, scope="batch")

        self._open_chapters[ci] = chapter

        def publish_when_polished() -> None:
            if self._polish_queue is not None and ci in self._polish_queue.pending_chapters():
                self._awaiting_polish.add(ci)
            else:
                self._publish_chapter(store, ci)

        if reopened_done:
            # Chapters reopened solely to recover polish already completed chapter extraction.
            publish_when_polished()
            return done

        src_text = "\n".join(s.source for s in text_segs)
        tgt_text = "\n".join(
            (s.target_before_polish if self._polish_queue is not None else None) or s.target or ""
            for s in text_segs
        )
        chapter_segments = text_segs

        def chapter_extracted(summary: dict[str, int]) -> None:
            if notes is not None:
                notes.maintain(ci, 0, chapter_segments, exclude=covered_notes)
            store.log_event("chapter_glossary_extracted", chapter=ci, summary=summary)
            publish_when_polished()

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

    def _publish_chapter(self, store: Storage, ci: int) -> None:
        """Publish a finished chapter after all its extraction and polish work has settled."""
        chapter = self._open_chapters.pop(ci, None) or store.load_chapter(ci)
        self._awaiting_polish.discard(ci)
        pipeline = self._runtime.config.pipeline
        publish_chapter_status(
            store,
            chapter,
            polish=pipeline.polish and pipeline.translation_mode == "standard",
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
            if is_resumable_provider_interrupt(error):
                raise
            outcome = PolishOutcome(job, None, error)
        self._apply_polish(store, outcome)

    def _apply_polish(self, store: Storage, outcome: PolishOutcome) -> None:
        """Apply one finished polish job to its in-memory chapter, then align and save."""
        job = outcome.job
        chapter = self._open_chapters[job.chapter]
        segments = chapter.text_segments[job.start_index : job.start_index + len(job.sources)]
        if tuple(segment.index for segment in segments) != job.segment_indices:
            raise RuntimeError("A polish job no longer matches its chapter segments")
        if outcome.error is not None and (
            not isinstance(outcome.error, Exception)
            or is_resumable_provider_interrupt(outcome.error)
        ):
            self._runtime.usage.flush(store, scope="batch")
            raise outcome.error

        polished_targets: list[str] | None = None
        if outcome.polished is not None and len(outcome.polished) == len(segments):
            polished_targets, _ = keep_nonempty_drafts(job.draft.targets, outcome.polished)

        applied = False
        for index, segment in enumerate(segments):
            eligible = bool(segment.meta.pop(POLISH_PENDING, False)) or bool(
                segment.meta.get(POLISH_FAILED, False)
            )
            if polished_targets is None:
                if eligible:
                    mark_polish_failure(segment, interrupted=False)
                continue
            if not eligible:
                continue
            # Only update segments still holding their saved draft; preserve manual edits.
            norm_before = (
                repair_escaped_newlines(segment.source, segment.target_before_polish)
                if segment.target_before_polish is not None
                else None
            )
            if norm_before is not None and segment.target != norm_before:
                segment.meta.pop(POLISH_FAILED, None)
                segment.meta.pop(POLISH_ATTEMPTS, None)
                continue
            segment.target = repair_escaped_newlines(segment.source, polished_targets[index])
            segment.meta.pop(POLISH_FAILED, None)
            segment.meta.pop(POLISH_ATTEMPTS, None)
            applied = True

        store.save_chapter(chapter)
        if applied or polished_targets is not None:
            if applied:
                self._align_batch_context(
                    store, chapter, chapter.text_segments, job.start_index, len(segments)
                )
            store.log_event(
                "batch_polished",
                chapter=job.chapter,
                start_index=job.start_index,
                count=len(segments),
            )
        else:
            reason = (
                f"{type(outcome.error).__name__}: {outcome.error}"
                if outcome.error is not None
                else "misaligned_output"
            )
            _LOGGER.warning(
                "Background polish failed for chapter %d batch %d (%s); kept draft.",
                job.chapter,
                job.start_index,
                reason,
            )
            store.log_event(
                "batch_polish_failed",
                chapter=job.chapter,
                start_index=job.start_index,
                count=len(segments),
                reason=reason,
            )

        if (
            job.chapter in self._awaiting_polish
            and self._polish_queue is not None
            and job.chapter not in self._polish_queue.pending_chapters()
        ):
            self._publish_chapter(store, job.chapter)
        self._runtime.usage.flush(store, scope="batch")

    def _align_batch_context(
        self, store: Storage, chapter: Chapter, segments: list[Segment], start: int, count: int
    ) -> None:
        """Align formal targets before subsequent batches derive their context."""
        self._annotations.align_annotations_after_batch(
            chapter.index, chapter, start, count, store, segments=segments
        )
        self._docx_styles.align_styles_after_batch(
            chapter.index, chapter, start, count, store, segments=segments
        )

    save_precision_batch = staticmethod(save_precision_batch)

    @staticmethod
    def chapter_progress_label(title: str, index: int) -> str:
        """Prefer the book's chapter title for progress."""
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
        """Extract terms after translating or resuming a batch for use by later batches."""
        src_text = "\n".join(s.source for s in batch)
        tgt_text = "\n".join(s.target or "" for s in batch)

        def stored(summary: dict[str, int]) -> None:
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
        """Store one extraction now, or overlap its model call with the next translation."""
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
        """Read the translated prefix backward, using drafts when background polish is active."""
        return recent_chapter_context(
            store,
            chapter,
            start,
            policy,
            max_tokens=self._runtime.config.pipeline.rolling_context_tokens,
            prefer_drafts=self._polish_queue is not None,
        )

    def following_source(
        self,
        store: Storage,
        chapter: Chapter,
        end: int,
        policy: BabeldocContext | None,
    ) -> str:
        """Find one following source reference in book order without translating it early."""
        return following_chapter_source(store, chapter, end, policy)
