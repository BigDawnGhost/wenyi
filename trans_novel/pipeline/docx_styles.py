"""DOCX 段内混排样式：仿 EPUB 注释，译后用标记对齐（整段同质不走此路径）。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..agents.annotation_aligner import AnnotationUnit, target_digest
from ..ingest.models import Chapter
from ..postprocess.punct import normalize_zh_segments
from .annotations import AnnotationService
from .runstore import RunStore

if TYPE_CHECKING:
    from .runtime import PipelineRuntime


def _style_fields(item: dict[str, Any]) -> dict[str, Any]:
    """从样式 item 中取出可写出的字符属性。"""
    out: dict[str, Any] = {}
    for key in ("bold", "italic", "underline", "color", "size_pt", "font"):
        if key in item:
            out[key] = item[key]
    return out


def proportional_range_placements(
    source: str,
    target: str,
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """把源文 range 按比例映到译文（对齐失败时的样式兜底，优于段末零宽）。"""
    source_length = len(source)
    target_length = len(target)
    placements: list[dict[str, Any]] = []
    for item in items:
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            continue
        start = item.get("source_start")
        end = item.get("source_end")
        if not isinstance(start, int) or not isinstance(end, int):
            continue
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
        placements.append(
            {
                "id": item_id,
                "mode": "range",
                "target_start": t_start,
                "target_end": t_end,
                "status": "fallback",
                "method": "proportional_source_range",
                **_style_fields(item),
            }
        )
    return placements


def _merge_style_onto_placements(
    items: list[dict[str, Any]],
    placements: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """对齐结果只含偏移；把源 item 上的 bold/color 等合并回去。"""
    by_id = {str(item.get("id")): item for item in items if isinstance(item.get("id"), str)}
    merged: list[dict[str, Any]] = []
    for placement in placements:
        item_id = placement.get("id")
        row = dict(placement)
        source_item = by_id.get(str(item_id)) if item_id is not None else None
        if isinstance(source_item, dict):
            row.update(_style_fields(source_item))
        # 样式 range 的段末零宽 fallback 无意义，留给调用方用比例重算
        merged.append(row)
    return merged


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
        # 整段同质走 docx_style，不应出现在 docx_styles
        if segment.meta.get("docx_style"):
            return
        raw_items = metadata.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            return
        items = [dict(item) for item in raw_items if isinstance(item, dict)]
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
            if result.used_fallback or any(
                row.get("method") == "paragraph_end" for row in raw_placements
            ):
                raw_placements = proportional_range_placements(source, target, items)
                used_fallback = True
            else:
                raw_placements = _merge_style_onto_placements(items, raw_placements)
                used_fallback = False
        except Exception as error:  # noqa: BLE001 - 样式失败不得挡住译文
            raw_placements = proportional_range_placements(source, target, items)
            used_fallback = True
            store.log_event(
                "docx_style_alignment_failed",
                chapter=ci,
                segment=segment.index,
                error=type(error).__name__,
                detail=str(error),
            )

        metadata["target_digest"] = target_digest(target)
        metadata["placements"] = raw_placements
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
