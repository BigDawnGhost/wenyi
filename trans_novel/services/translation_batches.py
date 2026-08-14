"""为翻译、续跑和审查生成不携带正文对象的稳定批次计划。

该模块只读取段落的 ``source``/``target`` 视图，并返回索引范围 DTO。
它不返回 ``Segment``、不访问 RunStore，也不写事件；调用方可用相同计划
定位内存章节或其他适配器的输入切片。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


class TranslationSegmentView(Protocol):
    """批次规划所需的最小只读段落接口。"""

    source: str
    target: str | None


@dataclass(frozen=True, slots=True)
class TranslationBatchPlan:
    """一个连续段落切片及其续跑完成状态。"""

    # 这里是输入序列（通常为 Chapter.text_segments）的位置，不是可能
    # 非连续的 Segment.index 领域编号。
    start_index: int
    count: int
    source_chars: int
    completed: bool

    @property
    def stop_index(self) -> int:
        """返回与 Python 切片一致的开区间结束位置。"""
        return self.start_index + self.count


@dataclass(frozen=True, slots=True)
class _SegmentFact:
    """规划期间使用的已校验小值，避免保留调用方段落对象。"""

    source_chars: int
    completed: bool


def plan_contiguous_batches(
    segments: Sequence[TranslationSegmentView],
    max_chars: int,
) -> tuple[TranslationBatchPlan, ...]:
    """按源文字符预算保序打包，不沿完成状态额外切分。"""
    facts = _segment_facts(segments)
    return _contiguous_plans(facts, max_chars)


def plan_resumable_batches(
    segments: Sequence[TranslationSegmentView],
    max_chars: int,
) -> tuple[TranslationBatchPlan, ...]:
    """先按预算打包，再沿已完成/待翻译边界切开每个原始批次。

    预算变化后，一个原始批次可能混合已有译文和空译文。续跑若重做整个
    混合批次会覆盖已确认内容，因此每个返回计划都只含一种完成状态。
    """
    facts = _segment_facts(segments)
    raw_plans = _contiguous_plans(facts, max_chars)
    resumable: list[TranslationBatchPlan] = []

    # 每个预算批次独立切分，绝不把相邻原始批次重新合并。
    for raw in raw_plans:
        group_start = raw.start_index
        group_chars = 0
        group_completed = facts[group_start].completed
        for index in range(raw.start_index, raw.stop_index):
            fact = facts[index]
            if index > group_start and fact.completed != group_completed:
                resumable.append(
                    TranslationBatchPlan(
                        start_index=group_start,
                        count=index - group_start,
                        source_chars=group_chars,
                        completed=group_completed,
                    )
                )
                group_start = index
                group_chars = 0
                group_completed = fact.completed
            group_chars += fact.source_chars
        resumable.append(
            TranslationBatchPlan(
                start_index=group_start,
                count=raw.stop_index - group_start,
                source_chars=group_chars,
                completed=group_completed,
            )
        )
    return tuple(resumable)


def _segment_facts(
    segments: Sequence[TranslationSegmentView],
) -> tuple[_SegmentFact, ...]:
    """校验最小段落视图，并复制出不会随调用方变化的规划事实。"""
    if isinstance(segments, (str, bytes)) or not isinstance(segments, Sequence):
        raise TypeError("segments 必须是段落序列")

    facts: list[_SegmentFact] = []
    for index, segment in enumerate(segments):
        source = getattr(segment, "source", None)
        target = getattr(segment, "target", None)
        if type(source) is not str:
            raise TypeError(f"segments[{index}].source 必须是字符串")
        if target is not None and type(target) is not str:
            raise TypeError(f"segments[{index}].target 必须是字符串或 None")
        facts.append(
            _SegmentFact(
                source_chars=len(source),
                completed=bool(target and target.strip()),
            )
        )
    return tuple(facts)


def _contiguous_plans(
    facts: tuple[_SegmentFact, ...],
    budget: int,
) -> tuple[TranslationBatchPlan, ...]:
    """执行旧版 O(n) 贪心打包，包括非正预算时的逐段退化语义。"""
    plans: list[TranslationBatchPlan] = []
    start_index = 0
    source_chars = 0

    for index, fact in enumerate(facts):
        if index > start_index and source_chars + fact.source_chars > budget:
            plans.append(
                _build_plan(
                    facts,
                    start_index=start_index,
                    stop_index=index,
                    source_chars=source_chars,
                )
            )
            start_index = index
            source_chars = 0
        source_chars += fact.source_chars

    if facts:
        plans.append(
            _build_plan(
                facts,
                start_index=start_index,
                stop_index=len(facts),
                source_chars=source_chars,
            )
        )
    return tuple(plans)


def _build_plan(
    facts: tuple[_SegmentFact, ...],
    *,
    start_index: int,
    stop_index: int,
    source_chars: int,
) -> TranslationBatchPlan:
    """把一个非空连续范围规范化为不可变计划。"""
    completed = all(fact.completed for fact in facts[start_index:stop_index])
    return TranslationBatchPlan(
        start_index=start_index,
        count=stop_index - start_index,
        source_chars=source_chars,
        completed=completed,
    )


__all__ = [
    "TranslationBatchPlan",
    "TranslationSegmentView",
    "plan_contiguous_batches",
    "plan_resumable_batches",
]
