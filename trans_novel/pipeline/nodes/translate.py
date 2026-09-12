"""章翻译节点：translate（批翻译 + 检查点）。"""

from __future__ import annotations

from copy import deepcopy

from trans_novel.config import Config
from trans_novel.epub.slots import source_passthrough_transport
from trans_novel.glossary.store import GlossaryStore
from trans_novel.pipeline.contracts import BatchCommitHook, NodeOutcome, NodeRequest
from trans_novel.pipeline.nodes.common import (
    chapter_term_snapshot,
    normalize_batch,
    record_lint,
    resume_batches,
    seed_chapter_context,
    source_context_before,
)
from trans_novel.pipeline.nodes.glossary import extract_and_store
from trans_novel.pipeline.nodes.translation_batch import (
    extract_batch_glossary,
    translate_batch,
)
from trans_novel.pipeline.planning import (
    frozen_input_fingerprint,
    preserve_source_input_fingerprint,
    translate_input_fingerprint,
    translation_model_profile,
    translation_structure_fingerprint_part,
)
from trans_novel.pipeline.quality import lint_targets
from trans_novel.pipeline.state import (
    NODE_TRANSLATE,
    SCOPE_CHAPTER,
    STATUS_DONE,
    PolishBatch,
    begin_translate,
    clear,
    stable_digest,
)


class TranslateNode:
    """逐章处理：语义保留原文，或执行批翻译与检查点。"""

    node_id = NODE_TRANSLATE
    scope = SCOPE_CHAPTER

    def __init__(
        self,
        *,
        translator,
        extractor,
        polisher,
        glossary: GlossaryStore,
        config: Config,
        style_brief: str,
        rolling_context,
        frozen_book=None,
        frozen_preparation=None,
        batch_commit_hook: BatchCommitHook | None = None,
    ):
        self.translator = translator
        self.extractor = extractor
        self.polisher = polisher
        self.glossary = glossary
        self.config = config
        self.style_brief = style_brief
        self.rolling_context = rolling_context
        self.frozen_book = frozen_book
        self.frozen_preparation = frozen_preparation
        self.batch_commit_hook = batch_commit_hook

    def execute(self, request: NodeRequest) -> NodeOutcome:
        ci = request.ci
        store = request.store
        chapter = store.load_chapter(ci)
        text_segs = chapter.text_segments
        if not text_segs:
            store.set_chapter_status(ci, STATUS_DONE)
            store.log_event("chapter_skipped", chapter=ci, reason="empty")
            fp = self._fingerprint("", store, chapter.processing, ci, request.shared)
            return NodeOutcome(chapter_finalized=True, fingerprint=fp)
        if chapter.preserve_source:
            return self._preserve_source(chapter, text_segs, store, request)
        return self._translate_regular(chapter, text_segs, store, request)

    def _preserve_source(self, chapter, text_segs, store, request) -> NodeOutcome:
        progress = store.load_progress(request.ci)
        if progress.pending_polish:
            raise ValueError(f"第{request.ci}章已标记保留原文，但仍有待润色译文")
        for segment in text_segs:
            segment.assign_translation(
                segment.source
                if segment.epub_state is None
                else source_passthrough_transport(segment.epub_state)
            )
        progress.pending_polish = []
        progress.lint_issues = []
        store.save_chapter(chapter)
        store.save_progress(request.ci, progress)
        request.shared.segments_done += len(text_segs)
        if request.progress:
            request.progress(
                request.shared.segments_done,
                request.shared.segments_total,
                f"第{request.ci}章 {chapter.title}",
            )
        store.log_event(
            "chapter_source_preserved",
            chapter=request.ci,
            reason=chapter.processing.reason,
            source_sha256=chapter.processing.source_sha256,
        )
        source_text = "\n".join(segment.source for segment in text_segs)
        return NodeOutcome(
            fingerprint=self._fingerprint(
                source_text, store, chapter.processing, request.ci, request.shared
            )
        )

    def _translate_regular(self, chapter, text_segs, store, request) -> NodeOutcome:
        ci, config = request.ci, self.config
        chapter_progress = store.load_progress(ci)
        glossary, context, style = self.glossary, self.rolling_context, self.style_brief
        seed_chapter_context(context, store, ci)
        batches = resume_batches(text_segs, config.segment.max_chars_per_batch)
        label = f"第{ci}章 {chapter.title}"
        if request.progress:
            request.progress(request.shared.segments_done, request.shared.segments_total, label)
        _, lint_issues = self._translate_batches(
            batches,
            text_segs,
            chapter,
            chapter_progress,
            store,
            request,
            glossary,
            context,
            style,
            label,
        )
        if config.pipeline.inflight_glossary and not config.pipeline.polish:
            src_text, tgt_text = (
                "\n".join(s.source for s in text_segs),
                "\n".join(s.target or "" for s in text_segs),
            )
            extract_and_store(self.extractor, glossary, src_text, tgt_text, ci)
            store.log_event("chapter_glossary_extracted", chapter=ci)
        chapter_progress.lint_issues = lint_issues
        store.save_progress(ci, chapter_progress)
        store.save_context(context.to_dict())
        fp = self._fingerprint(
            "\n".join(s.source for s in text_segs),
            store,
            chapter.processing,
            ci,
            request.shared,
        )
        return NodeOutcome(fingerprint=fp)

    def _translate_batches(
        self,
        batches,
        text_segs,
        chapter,
        chapter_progress,
        store,
        request,
        glossary,
        context,
        style,
        label,
    ):
        term_snapshot = chapter_term_snapshot(glossary, text_segs, self.config)
        lint_issues, seg_base = [], 0
        for batch in batches:
            existing = [s.target for s in batch if s.target and s.target.strip()]
            if len(existing) == len(batch):
                term_snapshot = self._resume_batch(
                    batch,
                    text_segs,
                    chapter_progress,
                    store,
                    request,
                    glossary,
                    context,
                    label,
                    term_snapshot,
                    lint_issues,
                    seg_base,
                )
            else:
                term_snapshot = self._translate_batch(
                    batch,
                    text_segs,
                    chapter,
                    chapter_progress,
                    store,
                    request,
                    glossary,
                    context,
                    style,
                    label,
                    term_snapshot,
                    lint_issues,
                    seg_base,
                )
            seg_base += len(batch)
        return term_snapshot, lint_issues

    def _resume_batch(
        self,
        batch,
        text_segs,
        chapter_progress,
        store,
        request,
        glossary,
        context,
        label,
        term_snapshot,
        lint_issues,
        seg_base,
    ):
        context.add_targets([s.target for s in batch])
        summary = None
        if self.config.pipeline.inflight_glossary:
            summary, changed = extract_batch_glossary(
                self.extractor, glossary, store, request.ci, seg_base, batch
            )
            remaining_src = "\n".join(s.source for s in text_segs[seg_base + len(batch) :])
            if changed and GlossaryStore.terms_in(changed, remaining_src):
                term_snapshot = chapter_term_snapshot(glossary, text_segs, self.config)
        record_lint(
            [s.source for s in batch],
            [s.target for s in batch],
            request.ci,
            seg_base,
            term_snapshot,
            lint_issues,
            src_lang=self.translator.src,
            store=None,
        )
        store.log_event(
            "batch_skipped",
            chapter=request.ci,
            start_index=seg_base,
            count=len(batch),
            reason="already_translated",
            glossary_extraction=summary,
            target_sha256=stable_digest([{"index": s.index, "target": s.target} for s in batch]),
        )
        request.shared.segments_done += len(batch)
        if request.progress:
            request.progress(request.shared.segments_done, request.shared.segments_total, label)
        return term_snapshot

    def _translate_batch(
        self,
        batch,
        text_segs,
        chapter,
        chapter_progress,
        store,
        request,
        glossary,
        context,
        style,
        label,
        term_snapshot,
        lint_issues,
        seg_base,
    ):
        raw_transports, call_count = translate_batch(
            self.translator,
            batch,
            term_snapshot,
            context,
            style,
            chapter_segments=text_segs,
            start_index=seg_base,
            chapter_title=chapter.title,
            n_recent=self.config.pipeline.rolling_context_segments,
            single_segment_translation=self.config.pipeline.single_segment_translation,
        )
        raw_targets = []
        for segment, transport in zip(batch, raw_transports, strict=True):
            segment.assign_translation(transport)
            raw_targets.append(segment.target or "")
        issues = lint_targets(
            [s.source for s in batch],
            raw_targets,
            locked_terms=[t for t in term_snapshot if getattr(t, "locked", 0)],
            src_lang=self.translator.src,
        )
        record_lint(
            [s.source for s in batch],
            raw_targets,
            request.ci,
            seg_base,
            term_snapshot,
            lint_issues,
            src_lang=self.translator.src,
            issues=issues,
            store=store,
        )
        request.shared.segments_done += len(batch)
        batch_start = seg_base
        if self.config.pipeline.polish:
            chapter_progress.pending_polish.append(PolishBatch(start=batch_start, count=len(batch)))
            event_targets = raw_targets
            normalized = False
        else:
            event_targets, normalized = normalize_batch(
                batch, raw_targets, punctuation_normalize=self.config.punctuation_normalize
            )
        chapter_progress.lint_issues = lint_issues
        self._commit_batch(
            batch,
            chapter,
            chapter_progress,
            store,
            request,
            batch_start,
            call_count,
            event_targets,
            normalized,
        )
        context.add_targets(event_targets)
        if self.config.pipeline.polish:
            request_terms = tuple(deepcopy(term_snapshot))
            future = request.executor.submit(
                self.polisher.polish,
                [s.target or "" for s in batch],
                [s.source for s in batch],
                glossary_terms=request_terms,
                style=style,
                source_context=source_context_before(text_segs, batch_start),
                chapter_title=chapter.title,
                strict=True,
            )
            request.shared.polish_futures[(request.ci, batch_start)] = (
                future,
                request_terms,
            )
        if self.config.pipeline.inflight_glossary:
            batch_src = "\n".join(s.source for s in batch)
            existing = GlossaryStore.terms_in(term_snapshot, batch_src)
            _summary, changed = extract_batch_glossary(
                self.extractor, glossary, store, request.ci, batch_start, batch, existing
            )
            remaining_src = "\n".join(s.source for s in text_segs[batch_start + len(batch) :])
            if changed and GlossaryStore.terms_in(changed, remaining_src):
                term_snapshot = chapter_term_snapshot(glossary, text_segs, self.config)
        if request.progress:
            request.progress(request.shared.segments_done, request.shared.segments_total, label)
        return term_snapshot

    def _commit_batch(
        self,
        batch,
        chapter,
        chapter_progress,
        store,
        request,
        batch_start,
        call_count,
        event_targets,
        normalized,
    ):
        if self.config.pipeline.polish:
            begin_translate(store, request.ci, batch_start, len(batch))
        store.save_chapter(chapter)
        store.save_progress(request.ci, chapter_progress)
        clear(store)
        event_payload = {
            "chapter": request.ci,
            "start_index": batch_start,
            "count": len(batch),
            "translate_call_count": call_count,
            "polished": False,
            "punctuation_normalized": normalized,
            "target_sha256": stable_digest(
                [
                    {"index": s.index, "target": t}
                    for s, t in zip(batch, event_targets, strict=False)
                ]
            ),
        }
        if self.batch_commit_hook is not None:
            store.log_event_required("batch_translated", **event_payload)
        else:
            store.log_event("batch_translated", **event_payload)
        if self.batch_commit_hook is not None:
            self.batch_commit_hook.after_batch_committed(request.ci, batch_start, len(batch))

    def _fingerprint(
        self,
        source_text: str,
        store,
        processing,
        chapter_index: int | None = None,
        shared=None,
    ) -> str:
        """Fingerprint text, EPUB slot geometry, and every model consumed by translation."""
        if chapter_index is not None:
            source_text += "\n" + translation_structure_fingerprint_part(
                store.load_chapter(chapter_index).text_segments
            )
        if processing is not None and processing.action == "preserve":
            return preserve_source_input_fingerprint(source_text, processing)
        if self.frozen_book is not None and self.frozen_preparation is not None:
            return frozen_input_fingerprint(
                self.frozen_preparation.preparation_sha256,
                self.node_id,
                (self.frozen_book.book_id, shared.frozen_chapter_index(chapter_index)),
                source_text,
            )
        state = store.load_state()
        return translate_input_fingerprint(
            source_text,
            state.identity.source_lang or self.config.source_lang,
            state.identity.target_lang or self.config.target_lang,
            style_brief=self.style_brief,
            punctuation_normalize=self.config.punctuation_normalize,
            honorific_strategy=self.config.honorific_strategy,
            glossary_scope=self.config.pipeline.glossary_scope,
            single_segment_translation=self.config.pipeline.single_segment_translation,
            model=translation_model_profile(self.config),
            processing=processing,
        )


__all__ = ["TranslateNode"]
