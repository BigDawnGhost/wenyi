"""为循环审校生成只读的单段临时替换候选。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from ..glossary.store import GlossaryTerm
from ..llm.json_parser import parse_json_result
from . import prompts
from .base import Agent

_OUTPUT_FIELDS = {
    "segment_ref",
    "before_hash",
    "issue_ids",
    "replacement",
    "complete",
}


class ReviewFixerProtocolError(ValueError):
    """Fixer 的输入或模型输出不符合临时补丁协议。"""


@dataclass(frozen=True)
class ProvisionalPatch:
    """尚未写入正式正文、只供下一轮 Review 验证的完整段落替换。"""

    patch_id: str
    round: int
    segment_ref: str
    chapter: int
    index: int
    before_hash: str
    before: str
    after: str
    issue_ids: tuple[str, ...]
    status: Literal["provisional"] = "provisional"

    def as_dict(self) -> dict[str, Any]:
        """返回可直接写入 Review 逐轮记录的稳定表示。"""
        return {
            "patch_id": self.patch_id,
            "round": self.round,
            "segment_ref": self.segment_ref,
            "chapter": self.chapter,
            "index": self.index,
            "before_hash": self.before_hash,
            "before": self.before,
            "after": self.after,
            "issue_ids": list(self.issue_ids),
            "status": self.status,
        }


def _sha256(text: str) -> str:
    """计算完整 UTF-8 文本的 SHA-256，供影子补丁做乐观校验。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _dialogue_quote_pairs(text: str) -> int:
    """粗略计数完整双引号对，用于阻止 Fixer 丢失既有对话边界。"""
    return (
        text.count('"') // 2
        + min(text.count("“"), text.count("”"))
        + min(text.count("「"), text.count("」"))
    )


def _patch_id(
    *,
    round_number: int,
    segment_ref: str,
    before_hash: str,
    after: str,
    issue_ids: tuple[str, ...],
) -> str:
    """根据补丁全部有效载荷生成可复现、内容敏感的 ID。"""
    payload = json.dumps(
        {
            "round": round_number,
            "segment_ref": segment_ref,
            "before_hash": before_hash,
            "after": after,
            "issue_ids": issue_ids,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"patch-r{round_number:02d}-{_sha256(payload)[:16]}"


def _nearby_text(pairs: Sequence[tuple[str, str]]) -> str:
    """把固定快照中的邻近原译文对渲染为只读上下文。"""
    if not pairs:
        return "（无）"
    rendered: list[str] = []
    for ordinal, pair in enumerate(pairs):
        if (
            not isinstance(pair, tuple)
            or len(pair) != 2
            or not all(isinstance(value, str) for value in pair)
        ):
            raise ReviewFixerProtocolError("invalid_nearby_pair")
        source, target = pair
        rendered.append(f"[上下文 {ordinal}] 原文：{source}\n    译文：{target}")
    return "\n".join(rendered)


def _glossary_text(
    relevant_glossary: Sequence[GlossaryTerm] | str,
) -> str:
    """渲染相关术语子集；也允许编排层传入预先本地化的只读文本。"""
    if isinstance(relevant_glossary, str):
        return relevant_glossary.strip() or "（无）"
    return prompts.render_glossary(list(relevant_glossary))


class ReviewFixer(Agent):
    """按已确认问题生成一条严格校验、不可直接落盘的完整段落补丁。"""

    @staticmethod
    def target_hash(target: str) -> str:
        """返回 Fixer 协议使用的当前译文哈希。"""
        if not isinstance(target, str):
            raise ReviewFixerProtocolError("invalid_current_target")
        return _sha256(target)

    @staticmethod
    def _issues(
        issues: Sequence[Mapping[str, Any]],
        *,
        chapter: int,
        index: int,
    ) -> tuple[tuple[str, ...], list[dict[str, Any]]]:
        """验证问题归属与指导字段，并生成最小、可序列化的提示载荷。"""
        if not issues:
            raise ReviewFixerProtocolError("issues_required")
        issue_ids: list[str] = []
        payload: list[dict[str, Any]] = []
        for issue in issues:
            if not isinstance(issue, Mapping):
                raise ReviewFixerProtocolError("invalid_issue")
            issue_id = issue.get("issue_id")
            detail = issue.get("detail")
            suggestion = issue.get("suggestion")
            if (
                not isinstance(issue_id, str)
                or not issue_id.strip()
                or not isinstance(detail, str)
                or not detail.strip()
                or not isinstance(suggestion, str)
                or not suggestion.strip()
            ):
                raise ReviewFixerProtocolError("invalid_issue")
            issue_id = issue_id.strip()
            if issue_id in issue_ids:
                raise ReviewFixerProtocolError("duplicate_issue_id")
            if "chapter" in issue and issue.get("chapter") != chapter:
                raise ReviewFixerProtocolError("issue_location_mismatch")
            if "index" in issue and issue.get("index") != index:
                raise ReviewFixerProtocolError("issue_location_mismatch")

            item: dict[str, Any] = {
                "issue_id": issue_id,
                "type": str(issue.get("type") or ""),
                "detail": detail.strip(),
                "suggestion": suggestion.strip(),
            }
            for field in ("consistency", "arbitration", "evidence_refs"):
                if field in issue:
                    item[field] = issue[field]
            issue_ids.append(issue_id)
            payload.append(item)
        return tuple(issue_ids), payload

    def propose(
        self,
        round_number: int,
        segment_ref: str,
        chapter: int,
        index: int,
        source: str,
        current_target: str,
        issues: Sequence[Mapping[str, Any]],
        *,
        style: str = "",
        book_synopsis: str = "",
        chapter_digest: str = "",
        relevant_glossary: Sequence[GlossaryTerm] | str = (),
        nearby_pairs: Sequence[tuple[str, str]] = (),
        trace: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> ProvisionalPatch:
        """生成单段临时替换，不写正文；协议错误会显式抛出。

        ``nearby_pairs`` 应来自同一轮不可变影子快照。模型只能参考这些上下文，
        输出仍必须是 ``source`` 所对应单段的完整译文。
        """
        if isinstance(round_number, bool) or not isinstance(round_number, int) or round_number < 1:
            raise ReviewFixerProtocolError("invalid_round")
        if not isinstance(segment_ref, str) or not segment_ref.strip():
            raise ReviewFixerProtocolError("invalid_segment_ref")
        segment_ref = segment_ref.strip()
        if isinstance(chapter, bool) or not isinstance(chapter, int) or chapter < 0:
            raise ReviewFixerProtocolError("invalid_chapter")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise ReviewFixerProtocolError("invalid_index")
        if not isinstance(source, str) or not source.strip():
            raise ReviewFixerProtocolError("empty_source")
        if not isinstance(current_target, str) or not current_target.strip():
            raise ReviewFixerProtocolError("empty_current_target")

        issue_ids, issue_payload = self._issues(issues, chapter=chapter, index=index)
        before_hash = self.target_hash(current_target)
        system = prompts.render(
            "review_fixer_system",
            src=self.src,
            tgt=self.tgt,
            lang_guidance=prompts.langprofile.translate_guidance(
                self.src,
                self.config.honorific_strategy,
            ),
        )
        user = prompts.render(
            "review_fixer_user",
            src=self.src,
            tgt=self.tgt,
            style=style.strip() if isinstance(style, str) and style.strip() else "（无）",
            book_synopsis=(
                book_synopsis.strip()
                if isinstance(book_synopsis, str) and book_synopsis.strip()
                else "（无）"
            ),
            chapter_digest=(
                chapter_digest.strip()
                if isinstance(chapter_digest, str) and chapter_digest.strip()
                else "（无）"
            ),
            glossary=_glossary_text(relevant_glossary),
            nearby_pairs=_nearby_text(nearby_pairs),
            issues_json=json.dumps(issue_payload, ensure_ascii=False, indent=2),
            segment_ref=segment_ref,
            before_hash=before_hash,
            issue_ids_json=json.dumps(issue_ids, ensure_ascii=False),
            source=source,
            current_target=current_target,
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        if trace:
            trace("request", {"messages": [dict(message) for message in messages]})
        try:
            raw = self.client.complete(
                messages,
                tier=self.config.pipeline.review_agent_tier,
                json_mode=True,
                stage=type(self).__name__,
            )
        except Exception as error:
            if trace:
                trace(
                    "error",
                    {
                        "error_type": type(error).__name__,
                        "error": str(error),
                    },
                )
            raise
        if trace:
            trace("response", {"raw_response": raw})
        try:
            parsed = parse_json_result(raw)
        except ValueError as error:
            raise ReviewFixerProtocolError("malformed_json") from error
        data = parsed.value
        if trace:
            trace(
                "parsed",
                {
                    "value": data,
                    "json_repaired": parsed.repaired,
                },
            )
        if not isinstance(data, dict):
            raise ReviewFixerProtocolError("response_not_object")
        if set(data) != _OUTPUT_FIELDS:
            raise ReviewFixerProtocolError("unexpected_fields")
        if not data or list(data)[-1] != "complete":
            raise ReviewFixerProtocolError("completion_marker_not_last")
        if data.get("complete") is not True:
            raise ReviewFixerProtocolError("completion_marker_missing")
        if data.get("segment_ref") != segment_ref:
            raise ReviewFixerProtocolError("segment_ref_mismatch")
        if data.get("before_hash") != before_hash:
            raise ReviewFixerProtocolError("before_hash_mismatch")

        returned_ids = data.get("issue_ids")
        if (
            not isinstance(returned_ids, list)
            or any(not isinstance(issue_id, str) for issue_id in returned_ids)
            or len(returned_ids) != len(set(returned_ids))
        ):
            raise ReviewFixerProtocolError("invalid_issue_ids")
        if set(returned_ids) != set(issue_ids):
            raise ReviewFixerProtocolError("issue_ids_mismatch")

        replacement = data.get("replacement")
        if not isinstance(replacement, str) or not replacement.strip():
            raise ReviewFixerProtocolError("empty_replacement")
        replacement = replacement.strip()
        if replacement == current_target.strip():
            raise ReviewFixerProtocolError("unchanged_replacement")
        required_quote_pairs = min(
            _dialogue_quote_pairs(source),
            _dialogue_quote_pairs(current_target),
        )
        if _dialogue_quote_pairs(replacement) < required_quote_pairs:
            raise ReviewFixerProtocolError("dropped_dialogue_quotes")

        return ProvisionalPatch(
            patch_id=_patch_id(
                round_number=round_number,
                segment_ref=segment_ref,
                before_hash=before_hash,
                after=replacement,
                issue_ids=issue_ids,
            ),
            round=round_number,
            segment_ref=segment_ref,
            chapter=chapter,
            index=index,
            before_hash=before_hash,
            before=current_target,
            after=replacement,
            issue_ids=issue_ids,
        )
