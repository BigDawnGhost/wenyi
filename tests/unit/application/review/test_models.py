"""Review 应用 DTO 与确定性投影的行为兼容合同。"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import FrozenInstanceError, dataclass

import pytest

from trans_novel.application.review import (
    ReviewRoundResult,
    review_conflict_records,
    review_content_digest,
    review_net_changes,
    review_overlay_digest,
    review_public_issues,
    review_unresolved_conflict_records,
    review_unresolved_fallback_count,
)
from trans_novel.pipeline import orchestrator


# 测试视图只实现投影端口，证明新模块不依赖 ingest 的具体章节类型。
@dataclass
class _Segment:
    index: int
    source: str
    target: str | None
    anchor: str | None = None
    kind: str = "paragraph"


@dataclass
class _Chapter:
    index: int
    text_segments: list[_Segment]


def _chapters() -> list[_Chapter]:
    """构造含空目标、锚点和非默认类型的最小正式正文。"""
    return [
        _Chapter(
            index=2,
            text_segments=[
                _Segment(7, "alpha", "甲", anchor="a"),
                _Segment(9, "beta", None, kind="dialogue"),
            ],
        )
    ]


def _digest(payload: object) -> str:
    """按历史紧凑 JSON 规则独立计算期望摘要。"""
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def test_orchestrator_reexports_the_extracted_private_objects() -> None:
    """旧模块私有导入继续指向新实现，迁移期间不破坏既有调用方。"""
    assert orchestrator._ReviewRoundResult is ReviewRoundResult
    assert orchestrator._review_overlay_digest is review_overlay_digest
    assert orchestrator._review_content_digest is review_content_digest
    assert orchestrator._review_net_changes is review_net_changes
    assert orchestrator._review_public_issues is review_public_issues
    assert orchestrator._review_conflict_records is review_conflict_records
    assert orchestrator._review_unresolved_conflict_records is review_unresolved_conflict_records
    assert orchestrator._review_unresolved_fallback_count is review_unresolved_fallback_count


def test_review_application_helpers_do_not_load_legacy_runtime_dependencies() -> None:
    """干净解释器调用全部公共 helper 后不得加载配置、LLM 或旧 pipeline。"""
    script = r"""
import sys
from dataclasses import dataclass
from trans_novel.application.review import (
    ReviewRoundResult,
    build_conflict_groups,
    review_conflict_records,
    review_content_digest,
    review_net_changes,
    review_overlay_digest,
    review_public_issues,
    review_unresolved_conflict_records,
    review_unresolved_fallback_count,
)

@dataclass
class Segment:
    index: int
    source: str
    target: str | None
    anchor: str | None = None
    kind: str = "paragraph"

@dataclass
class Chapter:
    index: int
    text_segments: list[Segment]

chapters = [Chapter(0, [Segment(0, "source", "target")])]
issues = [
    {"issue_id": "one", "issue_key": "one", "chapter": 0, "index": 0,
     "_chunk_id": "a", "agent_fallback": True,
     "consistency": {"key": "term:x", "proposed_value": "A"}},
    {"issue_id": "two", "issue_key": "two", "chapter": 1, "index": 0,
     "_chunk_id": "b",
     "consistency": {"key": "term:x", "proposed_value": "B"}},
]
groups = build_conflict_groups(issues)
ReviewRoundResult([], [], [], groups, groups, 1)
review_overlay_digest(chapters, {})
review_content_digest(chapters)
review_net_changes(chapters, {}, [], {})
review_public_issues(issues)
review_conflict_records(groups, [{}])
review_unresolved_conflict_records(issues)
review_unresolved_fallback_count(issues)

forbidden_prefixes = (
    "trans_novel.config",
    "trans_novel.llm",
    "trans_novel.pipeline",
)
loaded = sorted(
    name for name in sys.modules if name.startswith(forbidden_prefixes)
)
if loaded:
    raise SystemExit(f"unexpected application review dependencies: {loaded}")
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_review_round_result_keeps_the_frozen_envelope_contract() -> None:
    """单轮结果字段不可改绑，避免编排器在返回后替换确定性结果切片。"""
    result = ReviewRoundResult([], [], [], [], [], 0)

    with pytest.raises(FrozenInstanceError):
        result.fallback_agent_count = 1  # type: ignore[misc]


def test_review_digests_preserve_ordered_historical_payloads() -> None:
    """正文摘要和影子摘要覆盖的字段、空值规则与旧实现完全一致。"""
    chapters = _chapters()

    assert review_overlay_digest(chapters, {}) == _digest([[2, 0, "甲"], [2, 1, ""]])
    assert review_overlay_digest(chapters, {(2, 1): "乙"}) == _digest([[2, 0, "甲"], [2, 1, "乙"]])
    assert review_content_digest(chapters) == _digest(
        [[2, 0, 7, "a", "paragraph", "alpha", "甲"], [2, 1, 9, "", "dialogue", "beta", ""]]
    )


def test_net_changes_filters_invalid_history_and_sorts_output() -> None:
    """仅报告净变化；非法坐标、循环拒绝记录和重复问题键不会污染结果。"""
    chapters = _chapters()
    changes = review_net_changes(
        chapters,
        {(3, 1): "尾声", (2, 1): "乙", (2, 0): "甲"},
        [
            {"chapter": 2, "index": 1, "issue_keys": ["b", "a", "a"]},
            {"chapter": 2, "index": 1, "issue_keys": ["ignored"], "status": "rejected_cycle"},
            {"chapter": True, "index": 1, "issue_keys": ["invalid-bool"]},
        ],
        {(2, 1): {"status": "accepted"}},
    )

    assert changes == [
        {
            "chapter": 2,
            "index": 1,
            "suggested_target": "乙",
            "issue_keys": ["a", "b"],
            "review_result": "accepted",
        },
        {
            "chapter": 3,
            "index": 1,
            "suggested_target": "尾声",
            "issue_keys": [],
            "review_result": "provisional",
        },
    ]


def test_public_issues_are_deduplicated_sanitized_and_sorted() -> None:
    """公开视图按稳定键去重、舍弃内部字段，并拒绝 bool 冒充整数坐标。"""
    public = review_public_issues(
        [
            {"issue_key": "z", "chapter": 2, "index": 1, "detail": "old"},
            {
                "issue_key": "a",
                "chapter": 1,
                "index": 3,
                "type": "term",
                "detail": "detail",
                "suggestion": "fix",
                "_chunk_id": "private",
            },
            {"issue_key": "z", "chapter": 2, "index": 1, "detail": "new"},
            {"issue_key": "bad", "chapter": True, "index": 0},
        ]
    )

    assert public == [
        {
            "issue_key": "a",
            "chapter": 1,
            "index": 3,
            "type": "term",
            "detail": "detail",
            "suggestion": "fix",
        },
        {
            "issue_key": "z",
            "chapter": 2,
            "index": 1,
            "type": "",
            "detail": "new",
            "suggestion": "",
        },
    ]


def test_conflict_records_and_unresolved_fallbacks_keep_auditable_identity() -> None:
    """互斥建议可重建为未解决记录，降级计数则按叶块身份去重。"""
    issues = [
        {
            "issue_id": "i-1",
            "issue_key": "k-1",
            "chapter": 1,
            "index": 0,
            "_chunk_id": "chunk-a",
            "agent_fallback": True,
            "consistency": {"key": "term:name", "proposed_value": "阿明"},
            "evidence_refs": ["ref-2"],
        },
        {
            "issue_id": "i-2",
            "issue_key": "k-2",
            "chapter": 2,
            "index": 0,
            "_chunk_id": "chunk-b",
            "agent_fallback": True,
            "consistency": {"key": "term:name", "proposed_value": "明君"},
            "evidence_refs": ["ref-1"],
            "arbitration": {"reason": "证据仍不足"},
        },
        {"issue_id": "i-3", "_chunk_id": "chunk-a", "agent_fallback": True},
    ]

    conflicts = review_unresolved_conflict_records(issues)

    assert conflicts == [
        {
            "conflict_id": "review-conflict-0001",
            "consistency_key": "term:name",
            "issue_ids": ["i-1", "i-2"],
            "proposals": [
                {"issue_id": "i-1", "chapter": 1, "index": 0, "proposed_value": "阿明"},
                {"issue_id": "i-2", "chapter": 2, "index": 0, "proposed_value": "明君"},
            ],
            "arbitration": {
                "conflict_id": "review-conflict-0001",
                "consistency_key": "term:name",
                "issue_ids": ["i-1", "i-2"],
                "status": "unresolved",
                "recommended_value": "",
                "reason": "证据仍不足",
                "supported_issue_ids": ["i-1", "i-2"],
                "rejected_issue_ids": [],
                "evidence_refs": ["ref-1", "ref-2"],
            },
        }
    ]
    assert review_unresolved_fallback_count(issues) == 2
