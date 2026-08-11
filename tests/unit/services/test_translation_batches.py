"""翻译批次规划服务的顺序、预算和断点续跑合同。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import pytest

from trans_novel.services import (
    TranslationBatchPlan,
    plan_contiguous_batches,
    plan_resumable_batches,
)


@dataclass
class _Segment:
    """只实现规划端口的轻量测试段落。"""

    source: str
    target: str | None = None
    index: int = 0


def _spans(plans: Sequence[TranslationBatchPlan]) -> list[tuple[int, int, int, bool]]:
    """把 DTO 压成便于断言的 start/stop/chars/completed 元组。"""
    return [
        (plan.start_index, plan.stop_index, plan.source_chars, plan.completed) for plan in plans
    ]


def test_contiguous_plans_preserve_order_and_budget_boundaries() -> None:
    """达到预算可留在当前批次，下一段使其超限时才切开。"""
    segments = [_Segment("aa"), _Segment("bbb"), _Segment("c"), _Segment("dddddd")]

    plans = plan_contiguous_batches(segments, max_chars=5)

    # 超长单段保持完整；规划服务只描述范围，不拆写正文。
    assert _spans(plans) == [
        (0, 2, 5, False),
        (2, 3, 1, False),
        (3, 4, 6, False),
    ]


def test_resumable_plans_split_only_inside_each_raw_budget_batch() -> None:
    """混合完成状态会切开，但相邻预算批次不会因状态相同而回并。"""
    segments = [
        _Segment("aa", "已译一"),
        _Segment("bb", "已译二"),
        _Segment("c"),
        _Segment("dddd", "已译四"),
        _Segment("e", "已译五"),
    ]

    plans = plan_resumable_batches(segments, max_chars=5)

    assert _spans(plans) == [
        (0, 2, 4, True),
        (2, 3, 1, False),
        (3, 5, 5, True),
    ]


def test_start_index_uses_text_segment_position_not_domain_index() -> None:
    """空段过滤后即使领域 index 不连续，计划仍使用输入切片位置。"""
    segments = [
        _Segment("aa", index=10),
        _Segment("bb", index=30),
        _Segment("cc", index=90),
    ]

    plans = plan_contiguous_batches(segments, max_chars=4)

    assert [(plan.start_index, plan.stop_index) for plan in plans] == [(0, 2), (2, 3)]


def test_whitespace_target_is_pending_and_plan_is_detached() -> None:
    """空白译文不算完成，且调用方后改段落不会篡改既有计划。"""
    segments = [_Segment("a", "译文"), _Segment("b", "  ")]

    plans = plan_resumable_batches(segments, max_chars=10)
    segments[0].target = None

    assert _spans(plans) == [(0, 1, 1, True), (1, 2, 1, False)]


def test_empty_input_produces_no_batches() -> None:
    """空章节不制造零长度计划。"""
    assert plan_contiguous_batches([], max_chars=10) == ()
    assert plan_resumable_batches([], max_chars=10) == ()


@pytest.mark.parametrize("budget", [0, -1, True, 1.5, "10"])
def test_invalid_budget_is_rejected(budget: object) -> None:
    """公共服务拒绝会让批次边界含糊的预算。"""
    with pytest.raises(ValueError, match="max_chars"):
        plan_resumable_batches([_Segment("a")], budget)  # type: ignore[arg-type]


def test_invalid_segment_view_is_rejected() -> None:
    """规划器不接受缺少稳定字符串 source/target 的运行时对象。"""
    with pytest.raises(TypeError, match="source"):
        plan_contiguous_batches([object()], max_chars=10)  # type: ignore[list-item]
    with pytest.raises(TypeError, match="target"):
        plan_contiguous_batches([_Segment("a", target=1)], max_chars=10)  # type: ignore[arg-type]


def _legacy_resumable_spans(
    segments: Sequence[_Segment],
    max_chars: int,
) -> list[tuple[int, int]]:
    """保留抽取前算法的最小参考实现，用于行为等价门禁。"""
    raw_spans: list[tuple[int, int]] = []
    start = 0
    chars = 0
    for index, segment in enumerate(segments):
        if index > start and chars + len(segment.source) > max_chars:
            raw_spans.append((start, index))
            start = index
            chars = 0
        chars += len(segment.source)
    if segments:
        raw_spans.append((start, len(segments)))

    # 旧 _resume_batches 在每个预算批次内沿 target 完成状态切分。
    result: list[tuple[int, int]] = []
    for raw_start, raw_stop in raw_spans:
        group_start = raw_start
        status = bool(segments[group_start].target and segments[group_start].target.strip())
        for index in range(raw_start + 1, raw_stop):
            next_status = bool(segments[index].target and segments[index].target.strip())
            if next_status != status:
                result.append((group_start, index))
                group_start = index
                status = next_status
        result.append((group_start, raw_stop))
    return result


@pytest.mark.parametrize(
    ("segments", "budget"),
    [
        (
            [
                _Segment("aa", "甲"),
                _Segment("bb"),
                _Segment("cc", "丙"),
                _Segment("dd"),
            ],
            10,
        ),
        ([_Segment("oversized", "完成"), _Segment("x")], 3),
        ([_Segment("a", " "), _Segment("bb", "完成"), _Segment("c", "完成")], 3),
        ([_Segment("aa", "完成"), _Segment("bb", "完成"), _Segment("cc", "完成")], 4),
    ],
    ids=("alternating", "oversized", "blank-target", "budget-boundary"),
)
def test_resumable_planner_matches_pre_extraction_algorithm(
    segments: list[_Segment],
    budget: int,
) -> None:
    """表驱动锁定旧编排器在预算变化和多次状态切换下的边界。"""
    plans = plan_resumable_batches(segments, budget)

    assert [(plan.start_index, plan.stop_index) for plan in plans] == (
        _legacy_resumable_spans(segments, budget)
    )
