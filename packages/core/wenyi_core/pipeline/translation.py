"""Translation batches, resume, polishing, rolling context, glossary extraction and titles.
Use the prepared book synopsis while the caller holds the book lock. Process chapters and
batches serially. For each batch: translate, persist targets, align/persist annotations,
update context, append the batch event, extract/checkpoint glossary terms, update history,
then proceed.
At chapter end, perform fallback glossary extraction and publish model text plus done
through save_chapter_with_status. Normalize punctuation only in export copies. If targets
exist without a glossary checkpoint, extract missing terms without retranslating or
overwriting targets.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ..glossary.extractor import TranslatedSegmentEvidence
from ..glossary.store import GlossaryStore
from ..ingest.models import Segment
from ..storage.protocol import Storage
from .context import RollingContext
from .docx_styles import DocxStyleService
from .runstore import STATUS_DONE
from .title_translation import TitleTranslationService
from .translation_batch import BatchPlan, TranslationBatchExecutor, resume_batches

if TYPE_CHECKING:
    from .annotations import AnnotationService
    from .runtime import PipelineRuntime

ProgressFn = Callable[[int, int, str], None]


def _is_mineru_pdf(manifest: dict[str, Any]) -> bool:
    """True for MinerU PDF state (fmt=pdf without BabelDOC markers)."""
    if manifest.get("fmt") != "pdf":
        return False
    raw_meta = manifest.get("meta")
    meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
    return not bool(meta.get("babeldoc")) and meta.get("pdf_export") != "babeldoc"


class TranslationService:
    """Domain service for body translation, glossary extraction and chapter/TOC titles."""

    def __init__(self, runtime: PipelineRuntime, annotations: AnnotationService):
        self._runtime = runtime
        self._annotations = annotations
        self._docx_styles = DocxStyleService(runtime)
        self._titles = TitleTranslationService(runtime.title_translator)
        self._batches = TranslationBatchExecutor(runtime.translator, runtime.polisher)

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
        manifest = store.load_manifest()
        glossary = store
        context = RollingContext.from_dict(
            store.load_context() or {},
            min_recent_keep=max(40, self._runtime.config.pipeline.rolling_context_segments),
        )
        style = self._runtime.analyzer.style_brief(store.load_analysis() or {})
        allow_empty_translations = _is_mineru_pdf(manifest)

        if only_chapter is not None:
            targets = [only_chapter]
            progress_chapters = targets
        else:
            targets = store.pending_chapters()
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
        try:
            for ci in targets:
                done = self.translate_chapter(
                    ci,
                    store,
                    glossary,
                    context,
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
                store.save_context(context.to_dict())
                self._runtime.flush_usage(store, scope="chapter")
            # Translate chapter/TOC titles after the body; keep the original book title and use glossary names.
            if not store.pending_chapters():
                self._titles.run(store, glossary, progress=progress)
        finally:
            self._runtime.flush_usage(store, scope="translate")
        if progress and total:
            progress(total, total, "Translation complete")
        store.log_event("translate_run_finished", total_segments=total)
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
            for segment_index, segment in enumerate(chapter.text_segments):
                source_parts.append(segment.source)
                target = (segment.target or "").strip()
                if not target:
                    continue
                history[(chapter_index, segment_index)] = TranslatedSegmentEvidence(
                    chapter=chapter_index,
                    segment=segment_index,
                    source=segment.source,
                    target=target,
                )
        return history, "\n".join(source_parts)

    @staticmethod
    def update_translation_history(
        history: dict[tuple[int, int], TranslatedSegmentEvidence],
        chapter: int,
        start_index: int,
        segments,
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
            segments = store.load_chapter(ci).text_segments
            total += len(segments)
            for batch in resume_batches(
                segments, self._runtime.config.segment.max_tokens_per_batch
            ):
                if all(segment.target is not None for segment in batch):
                    done += len(batch)
        return total, done

    def translate_chapter(
        self,
        ci: int,
        store: Storage,
        glossary: Storage | GlossaryStore,
        context: RollingContext,
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
        chapter = store.load_chapter(ci)
        text_segs = chapter.text_segments
        if not text_segs:
            store.set_chapter_status(ci, STATUS_DONE)
            store.log_event("chapter_skipped", chapter=ci, reason="empty")
            return done
        chapter_digest = chapter.meta.get("source_digest", "")
        annotation_contexts = self._annotations.annotation_contexts_for_segments(
            text_segs,
            annotation_context_registry,
        )

        batches = resume_batches(text_segs, self._runtime.config.segment.max_tokens_per_batch)
        label = self.chapter_progress_label(chapter.title, ci)
        # Preparation often ends with a parsing label, but resume may first restore glossary terms.
        # Refresh at chapter start so the whole model call is not incorrectly labeled as source parsing.
        if progress:
            progress(done, total, label)
        glossary_checkpoints = store.completed_batch_glossary_keys(ci)
        # Read one glossary snapshot at chapter start and filter by source when scope is chapter.
        # Refresh lazily only if the glossary may have changed and another batch needs translation.
        # Fully checkpointed skips neither extract nor refresh. Saved translations lacking extraction
        # still extract and mark the snapshot stale, preserving resume completeness without redundant reads.
        term_snapshot = self.chapter_term_snapshot(glossary, text_segs)
        term_snapshot_stale = False

        # Process batches serially: render current context, translate and immediately append targets.
        # This preserves pronoun, term and voice continuity between batches within a chapter.
        # Skip saved complete batches on resume and reconstruct context without retranslating them.
        seg_base = 0  # Chapter-local index of this batch's first paragraph, used to map local issue indices.
        for b in batches:
            batch_start = seg_base
            glossary_key = store.batch_glossary_key(batch_start, len(b))
            if all(s.target is not None for s in b):
                # Reuse a batch translated at this position/context, rebuild rolling context and skip it.
                # Blank "" is a completed MinerU allowance; only None means not yet translated.
                self._annotations.align_annotations_after_batch(
                    ci,
                    chapter,
                    batch_start,
                    len(b),
                    store,
                )
                self._docx_styles.align_styles_after_batch(
                    ci,
                    chapter,
                    batch_start,
                    len(b),
                    store,
                )
                context.add_targets([s.target or "" for s in b])
                self.sync_context_chapter_prefix(
                    context,
                    text_segs,
                    batch_start + len(b),
                )
                if glossary_key in glossary_checkpoints:
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
                    )
                    glossary_checkpoints.add(glossary_key)
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
                seg_base += len(b)
                if progress:
                    progress(done, total, label)
                continue

            if term_snapshot_stale:
                term_snapshot = self.chapter_term_snapshot(glossary, text_segs)
                term_snapshot_stale = False

            ctx_text = context.render(self._runtime.config.pipeline.rolling_context_segments)
            next_index = batch_start + len(b)
            # Read the immediate source neighbor without changing batches or saved context.
            next_source = text_segs[next_index].source if next_index < len(text_segs) else ""
            plan = BatchPlan.capture(
                ci,
                batch_start,
                b,
                term_snapshot,
                ctx_text,
                style,
                book_synopsis,
                chapter_digest,
                annotation_contexts[batch_start : batch_start + len(b)],
                next_source,
                allow_empty_translations=allow_empty_translations,
            )
            result = self._batches.execute(plan, polish=self._runtime.config.pipeline.polish)
            for segment, target, before_polish in zip(b, result.targets, result.before_polish):
                segment.target = target
                segment.target_before_polish = before_polish
            # Persist translations incrementally so interruption resumes after this batch.
            store.save_chapter(chapter)
            # Handle only annotated logical paragraphs touched by this batch, in source order.
            # If the batch contains only an initial slice of a long paragraph, wait until its final
            # continuation finishes before merging and aligning.
            self._annotations.align_annotations_after_batch(
                ci,
                chapter,
                batch_start,
                len(b),
                store,
            )
            self._docx_styles.align_styles_after_batch(
                ci,
                chapter,
                batch_start,
                len(b),
                store,
            )
            context.add_targets([s.target or "" for s in b])
            self.sync_context_chapter_prefix(
                context,
                text_segs,
                batch_start + len(b),
            )
            store.log_event(
                "batch_translated",
                chapter=ci,
                start_index=batch_start,
                count=len(b),
                polished=self._runtime.config.pipeline.polish,
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
            )
            self.update_translation_history(translation_history, ci, batch_start, b)
            glossary_checkpoints.add(glossary_key)
            # The glossary may have changed; refresh before the next real translation, not after the final batch.
            term_snapshot_stale = True

        # Keep chapter-wide extraction as a fallback for address, speech and fixed expressions needing context.
        # Final review reads the stable glossary after the entire book finishes translating.
        src_text = "\n".join(s.source for s in text_segs)
        tgt_text = "\n".join(s.target or "" for s in text_segs)
        chapter_glossary_summary = self._runtime.extractor.extract_and_store(
            glossary,
            src_text,
            tgt_text,
            ci,
            history=translation_history.values(),
            before=(ci, len(text_segs)),
            source_corpus=source_corpus,
        )
        store.log_event(
            "chapter_glossary_extracted",
            chapter=ci,
            summary=chapter_glossary_summary,
        )

        store.save_chapter_with_status(chapter, STATUS_DONE)
        store.log_event(
            "chapter_done",
            chapter=ci,
            title=chapter.title,
            segment_count=len(text_segs),
        )
        return done

    def chapter_term_snapshot(self, glossary: Storage | GlossaryStore, text_segs) -> list:
        """Return the glossary snapshot for this chapter; call again after writes to refresh
        it.
        """
        terms = glossary.all_terms()
        if self._runtime.config.pipeline.glossary_scope != "chapter":
            return terms
        src_text = "\n".join(s.source for s in text_segs)
        hit = {t.source for t in GlossaryStore.terms_in(terms, src_text)}
        return [t for t in terms if t.source in hit]

    @staticmethod
    def chapter_progress_label(title: str, index: int) -> str:
        """Prefer the book's chapter title for progress so internal indices cannot contradict
        visible numbering.
        """
        title = (title or "").strip()
        return title or f"Chapter {index + 1}"

    def extract_batch_glossary(
        self,
        glossary: Storage | GlossaryStore,
        store: Storage,
        chapter: int,
        start_index: int,
        batch,
        translation_history: dict[tuple[int, int], TranslatedSegmentEvidence],
        source_corpus: str,
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
        )
        store.log_event(
            "batch_glossary_extracted",
            chapter=chapter,
            start_index=start_index,
            count=len(batch),
            summary=summary,
        )
        return summary

    @staticmethod
    def sync_context_chapter_prefix(
        context: RollingContext,
        segments: list[Segment],
        end: int,
    ) -> None:
        """Refresh recent context from the chapter's completed prefix.
        When an annotated logical paragraph spans batches, completing its final continuation
        can finalize earlier targets too. Copy those updates into context so the next batch
        sees current formal text.
        """
        prefix = segments[: max(0, min(end, len(segments)))]
        if not prefix or any(segment.target is None for segment in prefix):
            return
        targets = [segment.target or "" for segment in prefix]
        retained = min(len(targets), len(context.recent_targets))
        if retained:
            context.recent_targets[-retained:] = targets[-retained:]
