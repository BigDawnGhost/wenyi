"""Translation batches, resume, polishing, rolling context, glossary extraction and titles.
Use the prepared book synopsis while the caller holds the book lock. Process chapters and
batches serially. For each batch: translate, persist targets, align/persist annotations,
update context, append the batch event, extract/checkpoint glossary terms, update history,
then proceed.
With ``pipeline.polish_concurrency`` above 1, standard mode saves each draft with a durable
polish-pending marker and hands polishing to a bounded background pool. Finished polish jobs
are applied on this thread in submission order; a chapter is published as done only after
its jobs are applied. Rolling context and glossary extraction may therefore read drafts.
At chapter end, perform fallback glossary extraction and publish model text plus done
through save_chapter_with_status. If targets exist without a glossary checkpoint, extract
missing terms without retranslating or overwriting targets.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from ...agents.glossary_extractor import TranslatedSegmentEvidence
from ...agents.glossary_note import GlossaryNoteAgent
from ...config import PipelineConfig
from ...events import ProgressFn
from ...glossary.prompt_scope import prompt_terms
from ...glossary.store import GlossaryStore
from ...ingest.tokens import count_tokens
from ...markup.contexts import annotation_contexts_for_segments
from ...models.document import Chapter, Segment
from ...models.glossary import GlossaryTerm
from ...storage.protocol import Storage
from ...storage.run_store import STATUS_DONE
from ..annotate.docx_styles import DocxStyleService
from .babeldoc_context import BabeldocContext
from .context import render_recent_translations
from .glossary_notes import GlossaryNotes
from .polish_queue import PolishOutcome, PolishQueue
from .precision import PrecisionBatchExecutor, PrecisionError
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


def _is_mineru_pdf(manifest: dict[str, Any]) -> bool:
    """True for MinerU PDF state (fmt=pdf without BabelDOC markers)."""
    if manifest.get("fmt") != "pdf":
        return False
    raw_meta = manifest.get("meta")
    meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
    return not bool(meta.get("babeldoc")) and meta.get("pdf_export") != "babeldoc"


# Segment meta marker saved with a draft whose polish has not been applied yet.
POLISH_PENDING = "polish_pending"
# Segment meta marker saved when polish failed after retries/splitting so resume can re-polish it.
POLISH_FAILED = "polish_failed"


class TranslationService:
    """Domain service for body translation, glossary extraction and chapter/TOC titles."""

    def __init__(self, runtime: PipelineRuntime, annotations: AnnotationService):
        self._runtime = runtime
        self._annotations = annotations
        self._docx_styles = DocxStyleService(runtime)
        self._titles = TitleTranslationService(runtime.title_translator)
        self._batches = TranslationBatchExecutor(runtime.translator, runtime.polisher)
        self._precision = PrecisionBatchExecutor(runtime.client, runtime.config)
        self._polish_queue: PolishQueue | None = None
        # In-memory chapters this run still writes, including those awaiting polish results.
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
        """Translate chapters serially and persist usage/progress under the book lock.
        The caller restores languages, validates only_chapter and prepares the synopsis.
        This method performs body and title translation with restored context.
        """
        self._runtime.require_glossary_note_access()
        PipelineConfig.model_validate(self._runtime.config.pipeline.model_dump())
        manifest = store.load_manifest()
        glossary = store
        style = self._runtime.analyzer.style_brief(store.load_analysis() or {})
        allow_empty_translations = _is_mineru_pdf(manifest)

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
        translation_history, source_corpus = self.load_translation_inputs(store)
        annotation_context_registry = store.load_annotation_contexts()
        store.log_event(
            "translate_run_started",
            only_chapter=only_chapter,
            chapters=targets,
            total_segments=total,
            allow_empty_translations=allow_empty_translations,
        )
        if (
            pipeline.polish
            and pipeline.translation_mode == "standard"
            and pipeline.polish_concurrency > 1
        ):
            self._polish_queue = PolishQueue(
                self._batches.polish,
                lambda outcome: self._apply_polish(store, outcome),
                workers=pipeline.polish_concurrency,
            )
        try:
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
                    allow_empty_translations=allow_empty_translations,
                )
                self._runtime.usage.flush(store, scope="chapter")
            queue = self._polish_queue
            if queue is not None:
                if progress and total and len(queue):
                    progress(done, total, f"Polishing ({len(queue)} batches pending)")
                queue.drain()
            # Translate chapter/TOC titles after the body; keep the original book title and use glossary names.
            if not store.pending_chapters():
                self._titles.run(store, glossary, progress=progress)
                self._annotations.repair_fallbacks(store, progress=progress)
        except BaseException:
            if self._polish_queue is not None:
                # Unapplied jobs keep their durable marker and are repolished on resume.
                self._polish_queue.close(cancel=True)
            raise
        finally:
            self._polish_queue = None
            self._open_chapters.clear()
            self._awaiting_polish.clear()
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
    ) -> tuple[dict[tuple[int, int], TranslatedSegmentEvidence], str]:
        """Read chapters once to rebuild translated-history indices and concatenate the source
        corpus.
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
        term_snapshot = glossary.all_terms()
        term_snapshot_stale = False

        # Process batches serially, deriving context from the latest formal targets.
        # This preserves pronoun, term and voice continuity between batches within a chapter.
        # Skip saved complete batches on resume and reconstruct context without retranslating them.
        seg_base = 0  # Chapter-local index of this batch's first paragraph, used to map local issue indices.
        for b in batches:
            batch_start = seg_base
            glossary_key = store.batch_glossary_key(batch_start, len(b))
            if all(s.target is not None for s in b):
                # Reuse a batch translated at this position/context, rebuild rolling context and skip it.
                # Blank "" is a completed MinerU allowance; only None means not yet translated.
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
                if glossary_key in glossary_checkpoints or (
                    context_policy is not None
                    and self._glossary_checkpoint_covers(glossary_checkpoints, batch_start, len(b))
                ):
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
                result = self._batches.execute(plan, polish=self._runtime.config.pipeline.polish)
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
            # Persist targets before glossary extraction so interruption cannot leave terms ahead of text.
            self.extract_batch_glossary(
                glossary,
                store,
                ci,
                batch_start,
                b,
                translation_history,
                source_corpus,
                admission=notes,
            )
            if notes is not None:
                covered_notes.update(notes.maintain(ci, batch_start, b))
            self.update_translation_history(translation_history, ci, batch_start, b)
            glossary_checkpoints.add(glossary_key)
            # The glossary may have changed; refresh before the next real translation, not after the final batch.
            term_snapshot_stale = True
            if polish_job is not None:
                # Queue after extraction so terms come from the draft, not a later polish.
                self._schedule_polish(store, polish_job)

        # Keep chapter-wide extraction as a fallback for address, speech and fixed expressions needing context.
        # Final review reads the stable glossary after the entire book finishes translating.
        src_text = "\n".join(s.source for s in text_segs)
        # With background polishing, read drafts so the result does not depend on job timing.
        tgt_text = "\n".join(
            (s.target_before_polish if self._polish_queue is not None else None) or s.target or ""
            for s in text_segs
        )
        chapter_glossary_summary = self._runtime.extractor.extract_and_store(
            glossary,
            src_text,
            tgt_text,
            ci,
            history=translation_history.values(),
            before=(ci, len(text_segs)),
            source_corpus=source_corpus,
            **({"admit": notes.admit} if notes is not None else {}),
        )
        if notes is not None:
            notes.maintain(ci, 0, text_segs, exclude=covered_notes)
        store.log_event(
            "chapter_glossary_extracted",
            chapter=ci,
            summary=chapter_glossary_summary,
        )

        # Precision publication reloads the chapter; publish the object this loop last saved.
        self._open_chapters[ci] = chapter
        if self._polish_queue is not None and ci in self._polish_queue.pending_chapters():
            # Publish done only after this chapter's queued polish jobs are applied.
            self._awaiting_polish.add(ci)
            return done
        self._publish_chapter(store, ci)
        return done

    def _publish_chapter(self, store: Storage, ci: int) -> None:
        chapter = self._open_chapters.pop(ci)
        self._awaiting_polish.discard(ci)
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

        ``all`` sends the full snapshot while it fits the token budget, then keeps its
        append-stable leading run plus any matching later entries. ``relevant`` keeps only
        matching entries within the token budget.
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
            outcome = PolishOutcome(job, self._batches.polish(job))
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
            self._publish_chapter(store, job.chapter)

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
    ) -> dict[str, int]:
        """Extract terms immediately after translating or resuming a batch for use by later
        chapter batches.
        """
        src_text = "\n".join(s.source for s in batch)
        tgt_text = "\n".join(s.target or "" for s in batch)
        summary = self._runtime.extractor.extract_and_store(
            glossary,
            src_text,
            tgt_text,
            chapter,
            history=translation_history.values(),
            before=(chapter, start_index),
            source_corpus=source_corpus,
            **({"admit": admission.admit} if admission is not None else {}),
        )
        store.log_event(
            "batch_glossary_extracted",
            chapter=chapter,
            start_index=start_index,
            count=len(batch),
            summary=summary,
        )
        return summary

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
