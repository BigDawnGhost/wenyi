"""DOCX 段内混排样式：仿 EPUB 注释，译后用标记对齐（整段同质不走此路径）。

模型职责：每个样式跨度单独请求，只在不可变译文上插入位置标记。
加粗/斜体/颜色/字号等属性一律从原文 ``items`` 继承，不经模型。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..agents.annotation_aligner import AnnotationUnit, target_digest
from ..ingest.models import Chapter
from ..postprocess.punct import normalize_zh_segments
from .annotations import AnnotationService
from .runstore import RunStore

if TYPE_CHECKING:
    from .runtime import PipelineRuntime

# 写出时从原文 item 继承的字段（不含原文 font）
_INHERIT_KEYS = ("bold", "italic", "underline", "color", "size_pt")


def _style_fields(item: dict[str, Any]) -> dict[str, Any]:
    """从样式 item 中取出可写出的字符属性（继承自原文，非模型输出）。"""
    out: dict[str, Any] = {}
    for key in _INHERIT_KEYS:
        if key in item:
            out[key] = item[key]
    return out


def _needs_alignment(item: dict[str, Any]) -> bool:
    """仅对含加粗/斜体/下划线/颜色的跨度请求模型定位。"""
    return any(key in item for key in ("bold", "italic", "underline", "color"))


def proportional_range_placement(
    source: str,
    target: str,
    item: dict[str, Any],
) -> dict[str, Any] | None:
    """单个 span 的比例回退 placement。"""
    item_id = item.get("id")
    start = item.get("source_start")
    end = item.get("source_end")
    if not isinstance(item_id, str) or not item_id:
        return None
    if not isinstance(start, int) or not isinstance(end, int):
        return None
    source_length = len(source)
    target_length = len(target)
    if source_length <= 0:
        t_start = t_end = 0
    else:
        t_start = min(
            target_length,
            (start * target_length + source_length // 2) // source_length,
        )
        t_end = min(
            target_length,
            (end * target_length + source_length // 2) // source_length,
        )
        if t_end < t_start:
            t_end = t_start
    return {
        "id": item_id,
        "mode": "range",
        "target_start": t_start,
        "target_end": t_end,
        "status": "fallback",
        "method": "proportional_source_range",
        **_style_fields(item),
    }


def proportional_range_placements(
    source: str,
    target: str,
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """把源文 range 按比例映到译文（对齐失败时的样式兜底，优于段末零宽）。"""
    out: list[dict[str, Any]] = []
    for item in items:
        row = proportional_range_placement(source, target, item)
        if row is not None:
            out.append(row)
    return out


def _placement_usable(row: dict[str, Any]) -> bool:
    """LLM 对齐成功且非「段末零宽」占位。"""
    if row.get("status") == "fallback":
        return False
    if row.get("method") == "paragraph_end":
        return False
    start = row.get("target_start")
    end = row.get("target_end")
    return isinstance(start, int) and isinstance(end, int) and end >= start


def merge_align_results(
    source: str,
    target: str,
    items: list[dict[str, Any]],
    placements: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    """按 span 合并：成功的保留位置并从原文继承样式；失败的单独比例回退。

    返回 (placements, any_fallback)。
    """
    by_id = {str(item.get("id")): item for item in items if isinstance(item.get("id"), str)}
    place_by_id = {
        str(row.get("id")): dict(row) for row in placements if isinstance(row.get("id"), str)
    }
    merged: list[dict[str, Any]] = []
    any_fallback = False
    for item in items:
        item_id = item.get("id")
        if not isinstance(item_id, str):
            continue
        row = place_by_id.get(item_id)
        if row is not None and _placement_usable(row):
            out = {
                "id": item_id,
                "mode": "range",
                "target_start": row["target_start"],
                "target_end": row["target_end"],
                "status": row.get("status", "aligned"),
                "method": row.get("method", "llm_markers"),
                **_style_fields(item),
            }
            merged.append(out)
            continue
        fallback = proportional_range_placement(source, target, item)
        if fallback is not None:
            any_fallback = True
            merged.append(fallback)
    # 保持与 by_id 一致；若 align 多返回了未知 id 则忽略
    _ = by_id
    return merged, any_fallback


class DocxStyleService:
    """仅处理 ``meta.docx_styles.items`` 混排；``docx_style`` 整段同质不调用模型。"""

    def __init__(self, runtime: PipelineRuntime):
        self._runtime = runtime

    def align_segment_styles(
        self,
        ci: int,
        chapter: Chapter,
        start_position: int,
        store: RunStore,
    ) -> None:
        """对一个逻辑段（含 cont）做混排样式对齐并写回 meta。"""
        segments = chapter.text_segments
        if not 0 <= start_position < len(segments):
            return
        while start_position > 0 and segments[start_position].cont:
            start_position -= 1
        segment = segments[start_position]
        metadata = segment.meta.get("docx_styles")
        if not isinstance(metadata, dict):
            return
        if segment.meta.get("docx_style"):
            return
        raw_items = metadata.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            return
        items = [
            dict(item) for item in raw_items if isinstance(item, dict) and _needs_alignment(item)
        ]
        if not items:
            return

        logical_segments = [segment]
        cursor = start_position + 1
        while cursor < len(segments) and segments[cursor].cont:
            logical_segments.append(segments[cursor])
            cursor += 1
        if any(not (item.target and item.target.strip()) for item in logical_segments):
            return

        target_changed = False
        if self._runtime.punctuation_enabled():
            targets = [item.target or "" for item in logical_segments]
            normalized = normalize_zh_segments(
                targets,
                [item.cont for item in logical_segments],
            )
            target_changed = normalized != targets
            for item, value in zip(logical_segments, normalized):
                item.target = value

        source = "".join(item.source for item in logical_segments)
        target = "".join(item.target or "" for item in logical_segments)
        expected_ids = {str(item.get("id")) for item in items if isinstance(item.get("id"), str)}
        placements = metadata.get("placements")
        placement_ids = {
            str(item.get("id"))
            for item in placements or []
            if isinstance(item, dict) and item.get("id")
        }
        if (
            metadata.get("target_digest") == target_digest(target)
            and expected_ids
            and placement_ids == expected_ids
        ):
            if target_changed:
                store.save_chapter(chapter)
            return

        # 模型只看位置：每个 span 单独请求（AnnotationAligner 对 N>1 会拆开）
        align_items = []
        for item in items:
            item_id = item.get("id")
            start = item.get("source_start")
            end = item.get("source_end")
            if not isinstance(item_id, str) or not item_id:
                continue
            if not isinstance(start, int) or not isinstance(end, int):
                continue
            align_items.append(
                {
                    "id": item_id,
                    "mode": "range",
                    "source_start": start,
                    "source_end": end,
                }
            )
        if not align_items:
            return

        unit = AnnotationUnit(
            unit_id=f"docx-style:ch{ci}:{segment.anchor or segment.index}",
            source=source,
            target=target,
            items=tuple(align_items),
        )

        try:
            result = self._runtime.annotation_aligner.align_unit(unit)
            raw_placements = [dict(row) for row in result.placements]
            # 按 span 合并：成功保留 LLM 位置 + 原文样式；失败仅该 span 比例回退
            merged, any_fallback = merge_align_results(source, target, items, raw_placements)
            used_fallback = any_fallback
        except Exception as error:  # noqa: BLE001 - 样式失败不得挡住译文
            merged = proportional_range_placements(source, target, items)
            used_fallback = True
            store.log_event(
                "docx_style_alignment_failed",
                chapter=ci,
                segment=segment.index,
                error=type(error).__name__,
                detail=str(error),
            )

        metadata["target_digest"] = target_digest(target)
        metadata["placements"] = merged
        store.save_chapter(chapter)
        store.log_event(
            "docx_style_alignment_completed",
            chapter=ci,
            segment=segment.index,
            spans=len(items),
            used_fallback=used_fallback,
        )

    def align_styles_after_batch(
        self,
        ci: int,
        chapter: Chapter,
        start: int,
        count: int,
        store: RunStore,
    ) -> None:
        """处理当前批次内已译完且含混排样式的逻辑段。"""
        segments = chapter.text_segments
        for logical_start in AnnotationService.completed_logical_starts_in_range(
            segments, start, count
        ):
            segment = segments[logical_start]
            styles = segment.meta.get("docx_styles")
            if isinstance(styles, dict) and styles.get("items"):
                self.align_segment_styles(ci, chapter, logical_start, store)
