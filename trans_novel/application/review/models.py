"""Review 结果 DTO 与不依赖运行时状态的确定性投影函数。

本模块只接收章节、问题和补丁的只读视图，不执行 LLM 调用、文件写入或正文修改。
相同输入必须产生相同摘要与记录顺序，以便审校循环安全地检测停滞并断点续跑。
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


# 该冻结 DTO 是单轮审校的完整返回信封；列表内容由调用方拥有，字段引用不可改绑。
@dataclass(frozen=True)
class _ReviewRoundResult:
    """一次全书影子译文 Review 及冲突仲裁后的确定性结果。"""

    issues: list[dict[str, Any]]
    pre_arbitration_issues: list[dict[str, Any]]
    arbitration_superseded: list[dict[str, Any]]
    conflict_groups: list[dict[str, Any]]
    residual_conflicts: list[dict[str, Any]]
    fallback_agent_count: int


# 摘要只覆盖 Review 实际可见的有序内容；紧凑 JSON 编码是历史指纹格式的一部分。
def _review_overlay_digest(
    chapters,
    overrides: Mapping[tuple[int, int], str],
) -> str:
    """计算全书有效影子译文指纹，用于检测无进展与 A↔B 振荡。"""
    payload = [
        (
            chapter.index,
            text_index,
            overrides.get((chapter.index, text_index), segment.target or ""),
        )
        for chapter in chapters
        for text_index, segment in enumerate(chapter.text_segments)
    ]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _review_content_digest(chapters) -> str:
    """计算本次 Review 实际读取的正式正文摘要。"""
    payload = [
        (
            chapter.index,
            text_index,
            segment.index,
            segment.anchor or "",
            segment.kind,
            segment.source,
            segment.target or "",
        )
        for chapter in chapters
        for text_index, segment in enumerate(chapter.text_segments)
    ]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# 补丁投影只保留相对正式正文的净变化，并对位置和问题键排序以稳定报告输出。
def _review_net_changes(
    chapters,
    overrides: Mapping[tuple[int, int], str],
    patch_records: list[dict[str, Any]],
    active_patches: Mapping[tuple[int, int], dict[str, Any]],
) -> list[dict[str, Any]]:
    """把多轮影子补丁折叠成每段一条的最终修改建议。"""
    baseline = {
        (chapter.index, text_index): segment.target or ""
        for chapter in chapters
        for text_index, segment in enumerate(chapter.text_segments)
    }
    issue_keys_by_location: dict[tuple[int, int], set[str]] = {}
    for patch in patch_records:
        chapter = patch.get("chapter")
        index = patch.get("index")
        if (
            not isinstance(chapter, int)
            or isinstance(chapter, bool)
            or not isinstance(index, int)
            or isinstance(index, bool)
            or patch.get("status") == "rejected_cycle"
        ):
            continue
        keys = issue_keys_by_location.setdefault((chapter, index), set())
        keys.update(str(key) for key in patch.get("issue_keys", []) if isinstance(key, str) and key)

    changes: list[dict[str, Any]] = []
    for location, suggested_target in sorted(overrides.items()):
        if baseline.get(location) == suggested_target:
            continue
        active = active_patches.get(location) or {}
        changes.append(
            {
                "chapter": location[0],
                "index": location[1],
                "suggested_target": suggested_target,
                "issue_keys": sorted(issue_keys_by_location.get(location, set())),
                "review_result": str(active.get("status") or "provisional"),
            }
        )
    return changes


# 面向用户的列表主动丢弃内部字段与非法坐标；同一稳定键以最后一条记录为准。
def _review_public_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """裁剪内部审校字段，生成面向用户的稳定问题列表。"""
    public: dict[str, dict[str, Any]] = {}
    for issue in issues:
        issue_key = issue.get("issue_key")
        chapter = issue.get("chapter")
        index = issue.get("index")
        if (
            not isinstance(issue_key, str)
            or not issue_key
            or not isinstance(chapter, int)
            or isinstance(chapter, bool)
            or not isinstance(index, int)
            or isinstance(index, bool)
        ):
            continue
        public[issue_key] = {
            "issue_key": issue_key,
            "chapter": chapter,
            "index": index,
            "type": str(issue.get("type") or ""),
            "detail": str(issue.get("detail") or ""),
            "suggestion": str(issue.get("suggestion") or ""),
        }
    return sorted(
        public.values(),
        key=lambda issue: (issue["chapter"], issue["index"], issue["issue_key"]),
    )


# 冲突投影保持分组与仲裁的一一对应次序，供逐轮审计文件稳定序列化。
def _review_conflict_records(
    groups: list[dict[str, Any]],
    arbitrations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """把冲突组及对应仲裁结果序列化为稳定的逐轮记录。"""
    return [
        {
            "conflict_id": group["conflict_id"],
            "consistency_key": group["consistency_key"],
            "issue_ids": [issue["issue_id"] for issue in group["issues"]],
            "proposals": [
                {
                    "issue_id": issue["issue_id"],
                    "chapter": issue["chapter"],
                    "index": issue["index"],
                    "proposed_value": issue["consistency"]["proposed_value"],
                }
                for issue in group["issues"]
            ],
            "arbitration": arbitration,
        }
        for group, arbitration in zip(groups, arbitrations)
    ]


# 冲突分组沿用既有的 Unicode 兼容归一化规则，确保大小写或全角差异不制造假冲突。
def _normalized_conflict_value(value: Any) -> str:
    """把候选建议规整为仅用于等价比较的稳定文本。"""
    text = value.strip() if isinstance(value, str) else ""
    return unicodedata.normalize("NFKC", text).casefold().strip()


def build_conflict_groups(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """找出不同审校块对同一一致性主题提出的互斥值。"""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for issue in issues:
        consistency = issue.get("consistency")
        if not isinstance(consistency, dict):
            continue
        key_value = consistency.get("key")
        proposed_value = consistency.get("proposed_value")
        key = key_value.strip() if isinstance(key_value, str) else ""
        proposed = proposed_value.strip() if isinstance(proposed_value, str) else ""
        if key and proposed:
            grouped.setdefault(key, []).append(issue)

    conflicts: list[dict[str, Any]] = []
    for key, group in grouped.items():
        chunks = {issue.get("_chunk_id") for issue in group}
        values = {
            _normalized_conflict_value(issue.get("consistency", {}).get("proposed_value"))
            for issue in group
        }
        values.discard("")
        if len(chunks) < 2 or len(values) < 2:
            continue
        conflicts.append(
            {
                "consistency_key": key,
                "issues": group,
                "first_position": min(
                    (issue.get("chapter", -1), issue.get("index", -1)) for issue in group
                ),
            }
        )
    conflicts.sort(key=lambda item: (item["first_position"], item["consistency_key"]))
    for ordinal, conflict in enumerate(conflicts, 1):
        conflict["conflict_id"] = f"review-conflict-{ordinal:04d}"
    return conflicts


def _review_unresolved_conflict_records(
    issues: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """从最终未解决问题重建冲突记录，避免被最后一轮空结果掩盖。"""
    groups = build_conflict_groups(issues)
    arbitrations: list[dict[str, Any]] = []
    for group in groups:
        issue_ids = [str(issue["issue_id"]) for issue in group["issues"]]
        annotations = [
            issue.get("arbitration")
            for issue in group["issues"]
            if isinstance(issue.get("arbitration"), dict)
        ]
        reasons = [
            str(annotation.get("reason", "")).strip()
            for annotation in annotations
            if str(annotation.get("reason", "")).strip()
        ]
        evidence_refs = sorted(
            {
                str(ref)
                for issue in group["issues"]
                for ref in issue.get("evidence_refs", [])
                if isinstance(ref, str) and ref
            }
        )
        arbitrations.append(
            {
                "conflict_id": group["conflict_id"],
                "consistency_key": group["consistency_key"],
                "issue_ids": issue_ids,
                "status": "unresolved",
                "recommended_value": "",
                "reason": reasons[-1] if reasons else "最终未解决问题仍包含互斥建议。",
                "supported_issue_ids": issue_ids,
                "rejected_issue_ids": [],
                "evidence_refs": evidence_refs,
            }
        )
    return _review_conflict_records(groups, arbitrations)


# 降级次数按叶块身份去重；历史字段缺失时依次回退到问题稳定键和临时 ID。
def _review_unresolved_fallback_count(issues: list[dict[str, Any]]) -> int:
    """统计最终未解决问题中仍由降级 Agent 产生的独立审校块。"""
    return len(
        {
            str(issue.get("_chunk_id") or issue.get("issue_key") or issue.get("issue_id"))
            for issue in issues
            if issue.get("agent_fallback")
        }
    )


# 新代码使用无下划线名字；旧编排器继续导入原私有名字，二者保持同一对象身份。
ReviewRoundResult = _ReviewRoundResult
review_overlay_digest = _review_overlay_digest
review_content_digest = _review_content_digest
review_net_changes = _review_net_changes
review_public_issues = _review_public_issues
review_conflict_records = _review_conflict_records
review_unresolved_conflict_records = _review_unresolved_conflict_records
review_unresolved_fallback_count = _review_unresolved_fallback_count

__all__ = [
    "ReviewRoundResult",
    "build_conflict_groups",
    "review_conflict_records",
    "review_content_digest",
    "review_net_changes",
    "review_overlay_digest",
    "review_public_issues",
    "review_unresolved_conflict_records",
    "review_unresolved_fallback_count",
]
