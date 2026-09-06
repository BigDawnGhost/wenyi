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
from ..ingest.epub_reader import strip_ruby_markers
from ..ingest.models import Segment
from ..ingest.segmenter import batch_segments
from .context import RollingContext
from .docx_styles import DocxStyleService
from .runstore import STATUS_DONE, RunStore

if TYPE_CHECKING:
    from .annotations import AnnotationService
    from .runtime import PipelineRuntime

ProgressFn = Callable[[int, int, str], None]


def _resume_batches(segments, max_chars: int) -> list[list]:
    """Split character-budget batches again at completed/pending boundaries.
    A changed budget may mix saved translations and empty targets in one batch. Group by
    completion state to translate only missing paragraphs and avoid overwriting confirmed
    content.
    """
    batches: list[list] = []
    for raw_batch in batch_segments(segments, max_chars):
        current: list = []
        current_done: bool | None = None
        for segment in raw_batch:
            done = bool(segment.target and segment.target.strip())
            if current and done != current_done:
                batches.append(current)
                current = []
            current.append(segment)
            current_done = done
        if current:
            batches.append(current)
    return batches


class TranslationService:
    """Domain service for body translation, glossary extraction and chapter/TOC titles."""

    def __init__(self, runtime: PipelineRuntime, annotations: AnnotationService):
        self._runtime = runtime
        self._annotations = annotations
        self._docx_styles = DocxStyleService(runtime)

    def run(
        self,
        store: RunStore,
        *,
        book_synopsis: str,
        only_chapter: int | None = None,
        progress: ProgressFn | None = None,
    ) -> RunStore:
        """Translate chapters serially and persist usage/progress under the book lock.
        The caller restores languages, validates only_chapter and prepares the synopsis.
        This method performs body and title translation with restored context.
        """
        manifest = store.load_manifest()
        glossary = GlossaryStore(store.glossary_path)
        context = RollingContext.from_dict(
            store.load_context() or {},
            min_recent_keep=max(40, self._runtime.config.pipeline.rolling_context_segments),
        )
        style = self._runtime.analyzer.style_brief(store.load_analysis() or {})

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
        )
        try:
            with self._runtime.metric_stage("translate"):
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
                    )
                    store.save_context(context.to_dict())
                    self._runtime.flush_usage(store, scope="chapter")
                # Translate chapter/TOC titles after the body; keep the original book title and use glossary names.
                if not store.pending_chapters():
                    self.translate_titles(store, glossary, progress=progress)
        finally:
            glossary.close()
            self._runtime.flush_usage(store, scope="translate")
        if progress and total:
            progress(total, total, "Translation complete")
        store.log_event("translate_run_finished", total_segments=total)
        return store

    @staticmethod
    def load_translation_inputs(
        store: RunStore,
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

    def progress_counts(self, store: RunStore, chapter_indices: list[int]) -> tuple[int, int]:
        """Compute progress from batch checkpoints, starting resume at completed translation
        counts.
        Count a batch as done only when all its targets exist. Counting partial batches
        early would duplicate completion counts if the batch reruns.
        """
        total = 0
        done = 0
        for ci in chapter_indices:
            segments = store.load_chapter(ci).text_segments
            total += len(segments)
            for batch in _resume_batches(
                segments, self._runtime.config.segment.max_chars_per_batch
            ):
                if all(segment.target and segment.target.strip() for segment in batch):
                    done += len(batch)
        return total, done

    def translate_chapter(
        self,
        ci: int,
        store: RunStore,
        glossary: GlossaryStore,
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

        batches = _resume_batches(text_segs, self._runtime.config.segment.max_chars_per_batch)
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
            existing_targets = [s.target for s in b if s.target and s.target.strip()]
            if len(existing_targets) == len(b):
                # Reuse a batch translated at this position/context, rebuild rolling context and skip it.
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
            targets = self.process_batch(
                b,
                term_snapshot,
                ctx_text,
                style,
                book_synopsis,
                chapter_digest,
                annotation_contexts=annotation_contexts[batch_start : batch_start + len(b)],
            )
            for s, t in zip(b, targets):
                s.target = t
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

    def chapter_term_snapshot(self, glossary: GlossaryStore, text_segs) -> list:
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
        glossary: GlossaryStore,
        store: RunStore,
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
        if not prefix or any(not (segment.target and segment.target.strip()) for segment in prefix):
            return
        targets = [segment.target or "" for segment in prefix]
        retained = min(len(targets), len(context.recent_targets))
        if retained:
            context.recent_targets[-retained:] = targets[-retained:]

    def translate_titles(
        self,
        store: RunStore,
        glossary: GlossaryStore,
        progress: ProgressFn | None = None,
    ) -> None:
        """Translate logical chapter titles and NCX/NAV entries and update the manifest.
        For TOC entries linked to heading segments, reuse the complete translated heading.
        Batch remaining titles, persist each batch and resume only unfinished entries.
        Preserve the original book title.
        """
        from ..agents import prompts

        m = store.load_manifest()
        chapters = m.get("chapters", [])

        # Collapse titles to one line so embedded newlines cannot break numbered alignment.
        def _flat(s: object) -> str:
            """Normalize a title to one line without repeated whitespace."""
            return " ".join(str(s or "").split())

        raw_meta = m.get("meta")
        meta = raw_meta if isinstance(raw_meta, dict) else {}
        raw_toc_entries = meta.get("toc_entries", [])
        toc_entry_items = raw_toc_entries if isinstance(raw_toc_entries, list) else []
        toc_entries = [
            entry
            for entry in toc_entry_items
            if isinstance(entry, dict) and _flat(entry.get("title", ""))
        ]

        # Long headings may have continuation slices. Merge their complete translation by anchor,
        # and allow TOC reuse only for heading segments.
        anchor_targets: dict[str, tuple[str, str, str]] = {}
        loaded_chapters = {
            chapter.get("index"): store.load_chapter(chapter["index"])
            for chapter in chapters
            if isinstance(chapter.get("index"), int)
        }

        def flush_anchor(
            active_anchor: str | None,
            active_kind: str,
            complete: bool,
            source_parts: list[str],
            parts: list[str],
        ) -> None:
            """Merge translated continuations for one anchor into the index."""
            if active_anchor and active_kind == "heading" and complete and parts:
                anchor_targets[active_anchor] = (
                    active_kind,
                    "".join(source_parts),
                    "".join(parts),
                )

        for chapter in loaded_chapters.values():
            active_anchor: str | None = None
            active_kind = ""
            parts: list[str] = []
            source_parts: list[str] = []
            complete = True

            for segment in chapter.text_segments:
                if segment.anchor:
                    flush_anchor(
                        active_anchor,
                        active_kind,
                        complete,
                        source_parts,
                        parts,
                    )
                    active_anchor = segment.anchor
                    active_kind = segment.kind
                    parts = [segment.target] if segment.target else []
                    source_parts = [segment.source]
                    complete = bool(segment.target and segment.target.strip())
                elif segment.cont and active_anchor:
                    source_parts.append(segment.source)
                    if segment.target and segment.target.strip():
                        parts.append(segment.target)
                    else:
                        complete = False
                else:
                    flush_anchor(
                        active_anchor,
                        active_kind,
                        complete,
                        source_parts,
                        parts,
                    )
                    active_anchor = None
                    active_kind = ""
                    parts = []
                    source_parts = []
                    complete = True
            flush_anchor(
                active_anchor,
                active_kind,
                complete,
                source_parts,
                parts,
            )

        changed = False
        for entry in toc_entries:
            if entry.get("title_translated"):
                continue
            anchor = entry.get("segment_anchor")
            linked = anchor_targets.get(anchor) if isinstance(anchor, str) else None
            can_reuse = bool(linked and _flat(linked[1]) == _flat(entry.get("title")))
            target = linked[2] if linked and can_reuse else ""
            if target.strip():
                entry["title_translated"] = target.strip()
                changed = True

        entry_by_id = {
            entry.get("entry_id"): entry
            for entry in toc_entries
            if isinstance(entry.get("entry_id"), str)
        }

        def sync_chapter_titles() -> None:
            """Reuse the starting TOC node's translation for its logical chapter."""
            nonlocal changed
            for manifest_chapter in chapters:
                if manifest_chapter.get("title_translated"):
                    continue
                entry = entry_by_id.get(manifest_chapter.get("toc_entry_id"))
                translated = entry.get("title_translated") if isinstance(entry, dict) else None
                if isinstance(translated, str) and translated.strip():
                    manifest_chapter["title_translated"] = translated.strip()
                    changed = True

        sync_chapter_titles()

        # Spine-fallback chapters lack toc_entry_id. If their title is the first heading, reuse that
        # heading's body translation to avoid inconsistent independently translated titles.
        for manifest_chapter in chapters:
            if manifest_chapter.get("title_translated"):
                continue
            chapter = loaded_chapters.get(manifest_chapter.get("index"))
            if chapter is None:
                continue
            first_heading = next(
                (segment for segment in chapter.text_segments if segment.kind == "heading"),
                None,
            )
            if (
                first_heading is not None
                and first_heading.anchor
                and _flat(first_heading.source) == _flat(manifest_chapter.get("title"))
            ):
                target = anchor_targets.get(first_heading.anchor, ("", "", ""))[2]
                if target.strip():
                    manifest_chapter["title_translated"] = target.strip()
                    changed = True

        pending: list[dict[str, object]] = []
        for entry in toc_entries:
            if not entry.get("title_translated"):
                pending.append({"record": entry, "source": _flat(entry.get("title"))})
        for chapter in chapters:
            if (
                _flat(chapter.get("title"))
                and not chapter.get("title_translated")
                and not chapter.get("toc_entry_id")
            ):
                pending.append({"record": chapter, "source": _flat(chapter.get("title"))})

        if changed:
            store.save_manifest(m)
        if not pending:
            store.log_event("titles_skipped", reason="already_translated_or_reused")
            return
        if progress:
            progress(0, len(pending), "Translating chapter titles…")

        # Bound both title count and character count for large TOCs to avoid truncated JSON responses.
        batches: list[list[dict[str, object]]] = []
        current: list[dict[str, object]] = []
        current_chars = 0
        for item in pending:
            source = str(item["source"])
            if current and (len(current) >= 40 or current_chars + len(source) > 4000):
                batches.append(current)
                current = []
                current_chars = 0
            current.append(item)
            current_chars += len(source)
        if current:
            batches.append(current)

        completed = 0
        glossary_text = prompts.render_glossary(glossary.all_terms())
        for batch_index, batch in enumerate(batches):
            titles = [str(item["source"]) for item in batch]
            system = prompts.render(
                "title_translator_system",
                src=self._runtime.config.source_lang,
                tgt=self._runtime.config.target_lang,
                n=len(titles),
            )
            user = prompts.render(
                "title_translator_user",
                src=self._runtime.config.source_lang,
                tgt=self._runtime.config.target_lang,
                glossary=glossary_text,
                n=len(titles),
                numbered_titles=prompts.numbered(titles),
            )
            try:
                data = self._runtime.client.complete_json(
                    [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    tier="strong",
                    stage="title_translate",
                )
            except Exception as error:
                store.log_event(
                    "titles_translation_failed",
                    batch=batch_index,
                    count=len(titles),
                    error=repr(error),
                )
                raise
            out = data.get("titles") if isinstance(data, dict) else data
            if not isinstance(out, list) or len(out) != len(titles):
                store.log_event(
                    "titles_translation_rejected",
                    batch=batch_index,
                    reason="count_mismatch",
                    expected=len(titles),
                    actual=len(out) if isinstance(out, list) else None,
                )
                raise RuntimeError(
                    "Chapter/TOC title translation returned an invalid number of items: "
                    f"expected {len(titles)}, got "
                    f"{len(out) if isinstance(out, list) else 'non-list'}"
                )
            translated = [str(title).strip() for title in out]
            for item, target in zip(batch, translated):
                record = item["record"]
                if isinstance(record, dict):
                    record["title_translated"] = target or item["source"]
            sync_chapter_titles()
            store.save_manifest(m)
            store.log_event(
                "titles_translated",
                batch=batch_index,
                titles=[
                    {"source": source, "target": target}
                    for source, target in zip(titles, translated)
                ],
            )
            completed += len(batch)
            if progress:
                progress(completed, len(pending), "Translating chapter titles")

    def process_batch(
        self,
        batch,
        terms,
        ctx_text: str,
        style: str,
        book_synopsis: str = "",
        chapter_digest: str = "",
        annotation_contexts: list[list[dict[str, str]]] | None = None,
    ) -> list[str]:
        """Translate then polish one batch.
        Translate every paragraph in its own context without reusing text across positions.
        Inject the book synopsis and chapter digest as stable prefixes. Normalize
        punctuation on disposable export copies only. Model review runs separately after
        whole-book translation, not inside each batch.
        """
        sources = [s.source for s in batch]
        targets = self._runtime.translator.translate_batch(
            sources,
            glossary_terms=terms,
            style=style,
            context=ctx_text,
            book_synopsis=book_synopsis,
            chapter_digest=chapter_digest,
            annotation_contexts=annotation_contexts,
        )
        # Strip pronunciation markers accidentally copied from source into the model's translation.
        targets = [strip_ruby_markers(target) for target in targets]

        if self._runtime.config.pipeline.polish:
            for segment, target in zip(batch, targets):
                segment.target_before_polish = target
            polished = self._runtime.polisher.polish(targets, glossary_terms=terms, style=style)
            if len(polished) == len(targets):
                targets = polished
        else:
            # Retranslation after configuration changes must not retain an older pre-polish snapshot.
            for segment in batch:
                segment.target_before_polish = None

        return targets
