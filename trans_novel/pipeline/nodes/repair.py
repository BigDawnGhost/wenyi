"""Issue-level deterministic lint repair with a persisted ten-call budget."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from trans_novel.agents.base import WorkflowProtocolError
from trans_novel.epub.slots import (
    distribute_slot_translation,
    target_slot_transport,
    translation_text,
)
from trans_novel.llm.errors import LLM_FALLBACK_ERRORS
from trans_novel.pipeline.contracts import NodeOutcome, NodeRequest
from trans_novel.pipeline.nodes.common import chapter_term_snapshot
from trans_novel.pipeline.quality import LintIssue, lint_targets
from trans_novel.pipeline.state import NODE_REPAIR, SCOPE_BOOK, RepairIssue, stable_digest

_MAX_ATTEMPTS = 10


def issue_key(
    chapter: int, index: int, issue_type: str, detail: str, subject: str | None = None
) -> str:
    identity = subject if subject is not None else re.sub(r"\s+", " ", detail).strip()
    payload = (chapter, index, issue_type, identity)
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


class RepairNode:
    node_id = NODE_REPAIR
    scope = SCOPE_BOOK

    def __init__(self, *, translator, glossary, style_brief: str = "", config=None):
        self.translator = translator
        self.glossary = glossary
        self.style_brief = style_brief
        self.config = config

    def execute(self, request: NodeRequest) -> NodeOutcome:
        store = request.store
        state = store.load_state()
        for chapter in state.chapters:
            if chapter.processing is None or chapter.processing.action != "preserve":
                continue
            progress = store.load_progress(chapter.index)
            pending_repairs = any(
                issue.status in {"pending", "repairing"}
                for issue in progress.repair_ledger.values()
            )
            if progress.pending_polish or progress.lint_issues or pending_repairs:
                raise ValueError(f"第{chapter.index}章已标记保留原文，但仍有待处理的译文工作")
        if request.progress:
            request.progress(0, 0, "修复译文问题…")
        initial = self._initial_issues(request)
        self._register(initial, store)
        deferred = not self._repair_queue(store, request)
        if deferred and request.progress:
            request.progress(0, 0, "Repair 服务调用失败，继续生成译文文件…")
        if not deferred:
            while True:
                current = self._book_issues(store)
                eligible = self._eligible(current, store)
                if not eligible:
                    break
                self._register(current, store)
                deferred = not self._repair_queue(store, request)
                if deferred:
                    current = self._book_issues(store)
                    break
        else:
            current = self._book_issues(store)
        state = store.load_state()
        for chapter_meta in state.chapters:
            ci = chapter_meta.index
            progress = store.load_progress(ci)
            progress.lint_issues = [self._issue_dict(ci, item) for item in current.get(ci, [])]
            store.save_progress(ci, progress)
        current_keys = {
            issue_key(ci, item.index, item.type, item.detail)
            for ci, values in current.items()
            for item in values
        }
        for ci in state.progress:
            progress = store.load_progress(ci)
            changed = False
            for record in progress.repair_ledger.values():
                if record.status in {"pending", "repairing"} and record.key not in current_keys:
                    record.status = "resolved"
                    changed = True
            if changed:
                store.save_progress(ci, progress)
        ledger = self._ledger(store)
        exhausted = [
            {
                "key": item.key,
                "chapter": item.chapter,
                "index": item.index,
                "type": item.type,
                "detail": item.detail,
                "attempts": item.attempts,
            }
            for item in ledger.values()
            if item.status == "accepted_after_exhaustion"
        ]
        detected = len(ledger)
        attempts = sum(item.attempts for item in ledger.values())
        resolved = sum(item.status == "resolved" for item in ledger.values())
        artifacts = {
            "repair": {
                "detected": detected,
                "resolved": resolved,
                "accepted_after_exhaustion": len(exhausted),
                "attempts": attempts,
                "audit": exhausted,
                "deferred": deferred,
            },
            "issues": [
                self._issue_dict(ci, item) for ci, values in current.items() for item in values
            ],
        }
        store.record_node_output(NODE_REPAIR, artifacts)
        target_text = "\n".join(
            "\n".join(s.target or "" for s in store.load_chapter(ci).text_segments)
            for ci in sorted(current)
        )
        return NodeOutcome(
            artifacts=artifacts,
            findings_count=detected,
            fingerprint=stable_digest([target_text, attempts, detected]),
        )

    def _initial_issues(self, request: NodeRequest) -> dict[int, list[dict[str, Any]]]:
        state = request.store.load_state()
        preserved = {
            chapter.index
            for chapter in state.chapters
            if chapter.processing is not None and chapter.processing.action == "preserve"
        }
        payload = request.artifacts.get("deterministic_qa", {})
        raw = payload.get("issues") if isinstance(payload, dict) else None
        if raw is None:
            node = state.nodes.get("deterministic_qa")
            raw = (node.output or {}).get("issues", []) if node else []
        grouped: dict[int, list[dict[str, Any]]] = {}
        for item in raw or []:
            if (
                isinstance(item, dict)
                and isinstance(item.get("chapter"), int)
                and item["chapter"] not in preserved
            ):
                grouped.setdefault(item["chapter"], []).append(item)
        return grouped or self._book_issues(request.store)

    def _book_issues(self, store) -> dict[int, list[LintIssue]]:
        state = store.load_state()
        src_lang = state.identity.source_lang or getattr(self.translator, "src", "en") or "en"
        result: dict[int, list[LintIssue]] = {}
        for chapter_meta in state.chapters:
            ci = chapter_meta.index
            if chapter_meta.processing is not None and chapter_meta.processing.action == "preserve":
                continue
            chapter = store.load_chapter(ci)
            text_segments = chapter.text_segments
            terms = [term for term in self.glossary.all_terms() if getattr(term, "locked", 0)]
            found = lint_targets(
                [s.source for s in text_segments],
                [s.target or "" for s in text_segments],
                locked_terms=terms,
                src_lang=src_lang,
            )
            if found:
                result[ci] = [
                    LintIssue(text_segments[item.index].index, item.type, item.detail)
                    for item in sorted(found, key=lambda item: text_segments[item.index].index)
                ]
        return result

    def _register(self, grouped, store) -> None:
        for ci, values in grouped.items():
            progress = store.load_progress(ci)
            for raw in values:
                item = raw if isinstance(raw, dict) else self._issue_dict(ci, raw)
                index = int(item.get("index", 0))
                key = issue_key(ci, index, str(item.get("type", "")), str(item.get("detail", "")))
                if key not in progress.repair_ledger:
                    progress.repair_ledger[key] = RepairIssue(
                        key=key,
                        chapter=ci,
                        index=index,
                        type=str(item.get("type", "")),
                        detail=str(item.get("detail", "")),
                    )
                elif progress.repair_ledger[key].status == "resolved":
                    progress.repair_ledger[key].status = (
                        "accepted_after_exhaustion"
                        if progress.repair_ledger[key].attempts >= _MAX_ATTEMPTS
                        else "pending"
                    )
            store.save_progress(ci, progress)

    def _eligible(self, grouped, store) -> list[tuple[int, LintIssue]]:
        result = []
        for ci, values in grouped.items():
            progress = store.load_progress(ci)
            for item in values:
                key = issue_key(ci, item.index, item.type, item.detail)
                record = progress.repair_ledger.get(key)
                if record is None or record.status not in {"accepted_after_exhaustion", "resolved"}:
                    result.append((ci, item))
        return result

    def _repair_queue(self, store, request: NodeRequest) -> bool:
        while True:
            current = self._book_issues(store)
            queue = []
            for ci in sorted(current):
                progress = store.load_progress(ci)
                for item in current[ci]:
                    key = issue_key(ci, item.index, item.type, item.detail)
                    record = progress.repair_ledger.get(key)
                    if record and record.status != "accepted_after_exhaustion":
                        queue.append((ci, item, record))
            if not queue:
                return True
            ci, item, record = queue[0]
            if not self._attempt(ci, item, record, store, request):
                return False

    def _attempt(self, ci, target_issue, record, store, request) -> bool:
        chapter = store.load_chapter(ci)
        if chapter.preserve_source:
            raise ValueError(f"第{ci}章已标记保留原文，不能执行译文修复")
        segments = chapter.text_segments
        segment = next((s for s in segments if s.index == target_issue.index), None)
        if segment is None:
            record.status = "resolved"
            self._save_record(store, ci, record)
            return True
        config = getattr(request.shared, "config", None) or self.config
        state = store.load_state()
        src_lang = state.identity.source_lang or getattr(self.translator, "src", "en") or "en"
        terms = (
            chapter_term_snapshot(self.glossary, segments, config)
            if config
            else [term for term in self.glossary.all_terms() if getattr(term, "locked", 0)]
        )
        locked = [term for term in terms if getattr(term, "locked", 0)]
        current_target = self._target(segment)
        baseline = self._segment_issues(segment, locked, src_lang)
        baseline_keys = {issue_key(ci, segment.index, x.type, x.detail) for x in baseline}
        if record.key not in baseline_keys:
            record.status = "resolved"
            self._save_record(store, ci, record)
            return True
        if record.attempts >= _MAX_ATTEMPTS:
            record.status = "accepted_after_exhaustion"
            self._save_record(store, ci, record)
            return True
        record.attempts += 1
        record.status = "repairing"
        record.committed_target_fingerprint = stable_digest(current_target)
        self._save_record(store, ci, record)
        try:
            candidate = self.translator.repair_issue(
                segment.source,
                current_target,
                issue_type=target_issue.type,
                issue_detail=target_issue.detail,
                glossary_terms=locked,
                context_before=self._context(segments, segment, -1),
                context_after=self._context(segments, segment, 1),
            )
        except WorkflowProtocolError as exc:
            self._reject(record, store, ci, f"{type(exc).__name__}: {exc}")
            return True
        except LLM_FALLBACK_ERRORS as exc:
            self._reject(record, store, ci, f"{type(exc).__name__}: {exc}")
            return False
        try:
            candidate_text = candidate.strip() if isinstance(candidate, str) else ""
            if segment.epub_state is not None:
                transport = distribute_slot_translation(segment.epub_state, candidate_text)
                committed_text = translation_text(segment.epub_state, transport)
            else:
                transport = candidate_text
                committed_text = candidate_text
            candidate_keys = {
                issue_key(ci, segment.index, x.type, x.detail)
                for x in self._segment_issues_text(segment, committed_text, locked, src_lang)
            }
            if not candidate_text or record.key in candidate_keys or candidate_keys - baseline_keys:
                self._reject(record, store, ci, "candidate_rejected")
                return True
            segment.assign_translation(transport)
        except ValueError:
            self._reject(record, store, ci, "candidate_invalid")
            return True
        store.save_chapter(chapter)
        after = self._segment_issues(segment, locked, src_lang)
        after_keys = {issue_key(ci, segment.index, x.type, x.detail) for x in after}
        progress = store.load_progress(ci)
        for key, existing in progress.repair_ledger.items():
            if key in baseline_keys and key not in after_keys:
                existing.status = "resolved"
        record.status = "resolved" if record.key not in after_keys else "pending"
        record.last_detail = "accepted"
        self._save_record(store, ci, record, progress=progress)
        return True

    def _reject(self, record, store, ci, detail: str) -> None:
        record.last_detail = detail[:500]
        record.status = (
            "accepted_after_exhaustion" if record.attempts >= _MAX_ATTEMPTS else "pending"
        )
        self._save_record(store, ci, record)

    @staticmethod
    def _save_record(store, ci, record, *, progress=None) -> None:
        progress = progress or store.load_progress(ci)
        progress.repair_ledger[record.key] = record
        store.save_progress(ci, progress)

    @staticmethod
    def _target(segment) -> str:
        if segment.epub_state is None:
            return segment.target or ""
        return translation_text(segment.epub_state, target_slot_transport(segment.epub_state))

    @staticmethod
    def _segment_issues(segment, locked, src_lang):
        return lint_targets(
            [segment.source],
            [RepairNode._target(segment)],
            locked_terms=locked,
            src_lang=src_lang,
        )

    @staticmethod
    def _segment_issues_text(segment, target, locked, src_lang):
        return lint_targets(
            [segment.source],
            [target],
            locked_terms=locked,
            src_lang=src_lang,
        )

    @staticmethod
    def _context(segments, segment, delta: int) -> str:
        position = next((i for i, item in enumerate(segments) if item is segment), None)
        if position is None:
            return "（无）"
        index = position + delta
        if index < 0 or index >= len(segments):
            return "（无）"
        other = segments[index]
        source = other.source[:1000]
        target = RepairNode._target(other)[:1000]
        return f"原文：{source}\n译文：{target}"

    @staticmethod
    def _issue_dict(ci, item) -> dict[str, Any]:
        return {"chapter": ci, "index": item.index, "type": item.type, "detail": item.detail}

    @staticmethod
    def _ledger(store) -> dict[str, RepairIssue]:
        return {
            key: value
            for ci in store.load_state().progress.values()
            for key, value in ci.repair_ledger.items()
        }


__all__ = ["RepairNode", "issue_key"]
