"""Chapter-end polish draining and selection."""

from __future__ import annotations

import copy
from dataclasses import asdict

from trans_novel.agents import langprofile
from trans_novel.agents.polisher import PolishResult
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

_POLISH_AUDIT_STRATEGY = "checkpoint_batch_v1"


def _lint_evidence(issues) -> list[dict[str, str]]:
    return [{"type": issue.type, "detail": issue.detail} for issue in issues]


def _rejection_event(
    *,
    ci: int,
    segment,
    batch_start: int,
    batch_offset: int,
    raw: str,
    proposal: str | None,
    gate,
    reasons: list[str],
    locked_terms,
    configured_editor_models: list[str],
) -> dict:
    term_evidence = [asdict(term) for term in locked_terms]
    checked_proposal = gate.checked_proposal if proposal is not None else None
    proposal_issues = _lint_evidence(gate.proposal_issues) if proposal is not None else []
    identity = {
        "chapter": ci,
        "index": segment.index,
        "source": segment.source,
        "pre_polish_target": raw,
        "proposal": proposal,
        "reasons": reasons,
        "locked_terms": term_evidence,
        "strategy": _POLISH_AUDIT_STRATEGY,
    }
    return {
        "audit_id": stable_digest(identity),
        "chapter": ci,
        "index": segment.index,
        "batch_start": batch_start,
        "batch_offset": batch_offset,
        "resource_href": segment.resource_href,
        "anchor": segment.anchor,
        "source": segment.source,
        "pre_polish_target": raw,
        "proposal": proposal,
        "checked_raw": gate.checked_raw,
        "checked_proposal": checked_proposal,
        "raw_issues": _lint_evidence(gate.raw_issues),
        "proposal_issues": proposal_issues,
        "locked_terms": term_evidence,
        "reasons": reasons,
        "strategy": _POLISH_AUDIT_STRATEGY,
        "configured_editor_models": configured_editor_models,
        "source_sha256": stable_digest(segment.source),
        "pre_polish_target_sha256": stable_digest(raw),
        "proposal_sha256": stable_digest(proposal) if proposal is not None else None,
        "checked_raw_sha256": stable_digest(gate.checked_raw),
        "checked_proposal_sha256": (
            stable_digest(checked_proposal) if checked_proposal is not None else None
        ),
    }


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
        if chapter.preserve_source:
            if chapter_progress.pending_polish:
                raise ValueError("preserved chapter has pending polish work")
            return NodeOutcome()
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
        if self.config.pipeline.inflight_glossary:
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
                request_terms = tuple(copy.deepcopy(term_snapshot))
                future = executor.submit(
                    self.polisher.polish,
                    raw_plain,
                    [segment.source for segment in batch],
                    glossary_terms=request_terms,
                    style=style,
                    source_context=source_context_before(text_segs, start),
                    chapter_title=chapter.title,
                    strict=True,
                )
                futures_by_key[key] = (future, request_terms)

    def _resolve_result(
        self, future, eligible, raw_plain, request_terms, store, ci, start, count
    ) -> PolishResult:
        try:
            return future.result()
        except LLM_FALLBACK_ERRORS as exc:
            reason = type(exc).__name__
            store.log_event(
                "polish_batch_fallback",
                chapter=ci,
                start_index=start,
                count=count,
                reason=reason,
            )
            return PolishResult(
                texts=raw_plain,
                fallback_reasons=dict.fromkeys(eligible, reason),
                locked_terms=request_terms,
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
        """按批写回并即时落盘，确保续跑不丢润色。"""
        pending = list(chapter_progress.pending_polish)
        if not pending:
            return
        self._submit_pending(
            pending, chapter, text_segs, futures_by_key, executor, style, term_snapshot, ci
        )
        for entry in sorted(pending, key=lambda e: e.start):
            start, count = entry.start, entry.count
            submitted = futures_by_key.pop((ci, start), None)
            batch = text_segs[start : start + count]
            srcs = [segment.source for segment in batch]
            raw_plain = [segment.target or "" for segment in batch]
            eligible = {i for i, source in enumerate(srcs) if langprofile.needs_translation(source)}
            if submitted is None:
                raise ValueError("polish request missing after submission")
            future, request_terms = submitted
            polish_result = self._resolve_result(
                future, eligible, raw_plain, request_terms, store, ci, start, count
            )
            locked = [term for term in polish_result.locked_terms if term.locked]
            results = []
            selected_plain = []
            for i in range(count):
                if i not in eligible:
                    results.append(None)
                    selected_plain.append(srcs[i])
                    continue
                result = polish_gate(
                    srcs[i],
                    raw_plain[i],
                    polish_result.texts[i],
                    locked_terms=locked,
                    src_lang=self.polisher.src,
                    normalize_punctuation=self.config.punctuation_normalize,
                )
                results.append(result)
                selected_plain.append(result.selected)
            selected_transport = align_epub_translations(batch, selected_plain)
            for i, result in enumerate(results):
                if batch[i].epub_state is not None and self.config.punctuation_normalize:
                    selected_transport[i] = normalize_slot_transport(
                        batch[i].epub_state, selected_transport[i]
                    )
                if result is None:
                    continue
                fallback_reason = polish_result.fallback_reasons.get(i)
                accepted = result.accepted and fallback_reason is None
                if not accepted:
                    reasons = (
                        [fallback_reason]
                        if fallback_reason is not None
                        else list(result.rejection_reasons)
                    )
                    proposal = polish_result.proposals.get(i)
                    store.log_event_required(
                        "polish_rejected",
                        **_rejection_event(
                            ci=ci,
                            segment=batch[i],
                            batch_start=start,
                            batch_offset=i,
                            raw=raw_plain[i],
                            proposal=proposal,
                            gate=result,
                            reasons=reasons,
                            locked_terms=locked,
                            configured_editor_models=list(self.config.llm.models.editor),
                        ),
                    )
                self.polisher.client.usage.record_outcome(
                    "editor", "polish.batch", accepted=accepted
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
                polish_strategy=_POLISH_AUDIT_STRATEGY,
                target_sha256=stable_digest(
                    [
                        {"index": text_segs[start + i].index, "target": final[i]}
                        for i in range(count)
                    ]
                ),
            )


__all__ = ["PolishNode"]
