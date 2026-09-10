"""Chapter-end polish draining and selection."""

from __future__ import annotations

from trans_novel.agents import langprofile
from trans_novel.config import Config
from trans_novel.epub.slots import normalize_slot_transport
from trans_novel.glossary.store import GlossaryStore
from trans_novel.ingest.segmenter import batch_segments
from trans_novel.llm.errors import LLM_FALLBACK_ERRORS
from trans_novel.pipeline.contracts import NodeOutcome, NodeRequest
from trans_novel.pipeline.nodes.common import chapter_term_snapshot, source_context_before
from trans_novel.pipeline.nodes.glossary import extract_and_store
from trans_novel.pipeline.nodes.translation_batch import align_epub_translations
from trans_novel.pipeline.planning import (
    frozen_input_fingerprint,
    is_back_matter,
    polish_input_fingerprint,
    polish_model_profile,
)
from trans_novel.pipeline.quality import polish_gate
from trans_novel.pipeline.state import (
    NODE_POLISH,
    SCOPE_CHAPTER,
    PolishBatch,
    begin_polish,
    chapter_node_key,
    clear,
    stable_digest,
)


class PolishNode:
    """章末排干本章全部润色 future（本轮新提交的 + 续跑遗留的 pending_polish）。"""

    node_id = NODE_POLISH
    scope = SCOPE_CHAPTER

    def __init__(
        self,
        *,
        polisher,
        extractor,
        glossary: GlossaryStore,
        config: Config,
        style_brief: str,
        frozen_book=None,
        frozen_preparation=None,
    ):
        self.polisher = polisher
        self.extractor = extractor
        self.glossary = glossary
        self.config = config
        self.style_brief = style_brief
        self.frozen_book = frozen_book
        self.frozen_preparation = frozen_preparation

    def execute(self, request: NodeRequest) -> NodeOutcome:
        ci = request.ci
        store = request.store
        chapter = store.load_chapter(ci)
        chapter_progress = store.load_progress(ci)
        text_segs = chapter.text_segments
        term_snapshot = chapter_term_snapshot(self.glossary, text_segs, self.config)
        pending = list(chapter_progress.pending_polish)
        if not pending:
            # 策略从禁用切到启用：此前 polish 关闭时翻译的章没有 pending 标记，
            # 也没有记录过润色指纹（skipped/从未润色）。从已译段推导批次一次性补润色。
            node = store.load_state().nodes.get(chapter_node_key(NODE_POLISH, ci))
            if node is None or not node.input_fingerprint:
                pending = []
                idx = 0
                for b in batch_segments(text_segs, self.config.segment.max_chars_per_batch):
                    pending.append(PolishBatch(start=idx, count=len(b)))
                    idx += len(b)
                chapter_progress.pending_polish = list(pending)
        self._drain_chapter_polish(
            chapter,
            chapter_progress,
            text_segs,
            request.shared.polish_futures,
            request.executor,
            self.style_brief,
            term_snapshot,
            store,
            ci,
        )
        if self.config.pipeline.inflight_glossary and not is_back_matter(
            chapter.title, index=ci, total=request.total_chapters
        ):
            src_text = "\n".join(s.source for s in text_segs)
            tgt_text = "\n".join(s.target or "" for s in text_segs)
            extract_and_store(self.extractor, self.glossary, src_text, tgt_text, ci)
            store.log_event("chapter_glossary_extracted", chapter=ci)
        source_text = "\n".join(s.source for s in text_segs)
        if self.frozen_book is not None and self.frozen_preparation is not None:
            fp = frozen_input_fingerprint(
                self.frozen_preparation.preparation_sha256,
                self.node_id,
                (self.frozen_book.book_id, request.shared.frozen_chapter_index(ci)),
                source_text,
            )
        else:
            state = store.load_state()
            source_lang = state.identity.source_lang or self.config.source_lang
            fp = polish_input_fingerprint(
                source_text,
                source_lang,
                self.style_brief,
                punctuation_normalize=self.config.punctuation_normalize,
                model=polish_model_profile(self.config),
            )
        return NodeOutcome(fingerprint=fp)

    def _submit_pending(
        self, pending, chapter, text_segs, futures_by_key, executor, style, term_snapshot, ci
    ) -> None:
        for entry in pending:
            start = entry.start
            key = (ci, start)
            if key not in futures_by_key:
                count = entry.count
                batch = text_segs[start : start + count]
                raw_plain = [segment.target or "" for segment in batch]
                futures_by_key[key] = executor.submit(
                    self.polisher.polish,
                    raw_plain,
                    [segment.source for segment in batch],
                    glossary_terms=list(term_snapshot),
                    style=style,
                    source_contexts=tuple(
                        source_context_before(text_segs, index)
                        for index in range(start, start + count)
                    ),
                    chapter_title=chapter.title,
                    strict=True,
                )

    def _drain_chapter_polish(
        self,
        chapter,
        chapter_progress,
        text_segs,
        futures_by_key: dict,
        executor,
        style: str,
        term_snapshot,
        store,
        ci: int,
    ) -> None:
        """写回按批序进行；每批清完立即落盘，保证续跑不丢润色（不变量 b）。
        异常时结果回退 raw，不阻断整章。"""
        pending = list(chapter_progress.pending_polish)
        if not pending:
            return
        self._submit_pending(
            pending, chapter, text_segs, futures_by_key, executor, style, term_snapshot, ci
        )
        for entry in sorted(pending, key=lambda e: e.start):
            start, count = entry.start, entry.count
            fut = futures_by_key.pop((ci, start), None)
            batch = text_segs[start : start + count]
            srcs = [segment.source for segment in batch]
            raw_plain = [segment.target or "" for segment in batch]
            try:
                final_plain = fut.result() if fut is not None else raw_plain
            except LLM_FALLBACK_ERRORS as exc:
                final_plain = raw_plain
                store.log_event(
                    "polish_batch_fallback",
                    chapter=ci,
                    start_index=start,
                    count=count,
                    reason=type(exc).__name__,
                )
            locked = [t for t in term_snapshot if getattr(t, "locked", 0)]
            results = [
                polish_gate(
                    srcs[i],
                    raw_plain[i],
                    final_plain[i],
                    locked_terms=locked,
                    src_lang=self.polisher.src,
                    normalize_punctuation=self.config.punctuation_normalize,
                )
                for i in range(count)
            ]
            selected_plain = [
                srcs[i] if not langprofile.needs_translation(srcs[i]) else result.selected
                for i, result in enumerate(results)
            ]
            selected_transport = align_epub_translations(batch, selected_plain)
            for i, result in enumerate(results):
                if batch[i].epub_state is not None and self.config.punctuation_normalize:
                    selected_transport[i] = normalize_slot_transport(
                        batch[i].epub_state, selected_transport[i]
                    )
                if result.accepted:
                    self.polisher.client.usage.record_outcome(
                        "editor", "polish.segment", accepted=True
                    )
                    continue
                self.polisher.client.usage.record_outcome(
                    "editor", "polish.segment", accepted=False
                )
                store.log_event(
                    "polish_rejected",
                    chapter=ci,
                    index=start + i,
                    reason=list(result.rejection_reasons),
                    proposal_sha256=stable_digest(result.proposal),
                )
            final = []
            for segment, value in zip(batch, selected_transport, strict=True):
                segment.assign_translation(value)
                final.append(segment.target or "")
            chapter_progress.pending_polish = [
                e for e in chapter_progress.pending_polish if e.start != start
            ]
            changes = [
                {
                    "index": text_segs[start + i].index,
                    "before": raw_plain[i],
                    "after": final[i],
                }
                for i in range(count)
                if raw_plain[i] != final[i]
            ]
            begin_polish(store, ci, start, count, final)
            store.save_chapter(chapter)
            store.save_progress(ci, chapter_progress)
            clear(store)
            store.log_event(
                "batch_polished",
                chapter=ci,
                start_index=start,
                count=count,
                changed_count=len(changes),
                changes=changes,
                target_sha256=stable_digest(
                    [
                        {"index": text_segs[start + i].index, "target": final[i]}
                        for i in range(count)
                    ]
                ),
            )


__all__ = ["PolishNode"]
