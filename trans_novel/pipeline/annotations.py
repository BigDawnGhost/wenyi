"""注释服务：EPUB 注释上下文映射、续段重组、串行定位与失败降级。

负责：
  * 注释上下文到切片的映射（超长段切分后的 point/range 偏移分配）；
  * 注释逻辑段重组：最后一个 cont 续段译完后才合并完整 source/target；
  * 注释定位、placement 缓存（target_digest 幂等）、中文标点定稿及失败降级。
多个注释逻辑段保持严格串行；每段完成后立即持久化；定位异常仅记录事件并继续。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..agents.annotation_aligner import AnnotationUnit, target_digest
from ..ingest.models import Chapter, Segment
from ..postprocess.punct import normalize_zh_segments
from .runstore import RunStore

if TYPE_CHECKING:
    from .runtime import PipelineRuntime


class AnnotationService:
    """注释定位与续段重组的领域服务。"""

    def __init__(self, runtime: PipelineRuntime):
        self._runtime = runtime

    @staticmethod
    def completed_logical_starts_in_range(
        segments: list[Segment],
        start: int,
        count: int,
    ) -> list[int]:
        """返回最后一片落在当前批次内的逻辑原段起点，保持顺序并去重。

        超长原段可能被切成首段和多个 cont 续段，且切分后的翻译批次
        可能刚好从续段开始。向前追溯到首段，才能在最后一个续段译完时立即
        合并完整 source/target 并执行一次注释定位。只在逻辑段末片属于当前
        范围时返回，避免同一组续段跨多个批次时重复处理。
        """
        if count <= 0 or not segments:
            return []
        lower = max(0, start)
        upper = min(len(segments), lower + count)
        starts: list[int] = []
        position = lower
        while position < upper:
            logical_start = position
            while logical_start > 0 and segments[logical_start].cont:
                logical_start -= 1
            logical_end = logical_start
            while logical_end + 1 < len(segments) and segments[logical_end + 1].cont:
                logical_end += 1
            if lower <= logical_end < upper:
                starts.append(logical_start)
            position = max(position + 1, logical_end + 1)
        return starts

    @staticmethod
    def annotation_contexts_for_segments(
        segments: list[Segment],
        registry: dict[str, Any] | None,
    ) -> list[list[dict[str, str]]]:
        """按源文偏移把书级注释原文分配给对应的实际翻译切片。

        EPUB 布局元数据只保存在一个逻辑段的首片；超长段的 cont
        续片没有独立 metadata。这里使用首片记录的原始字符偏移和各切片
        累计边界，把 point 注释分给所在切片、range 注释分给所有相交
        切片。相同目标在同一切片只注入一次。
        """
        assigned: list[list[dict[str, str]]] = [[] for _ in segments]
        if not isinstance(registry, dict):
            return assigned
        raw_contexts = registry.get("contexts")
        if not isinstance(raw_contexts, dict):
            return assigned

        position = 0
        while position < len(segments):
            logical_start = position
            logical_end = logical_start + 1
            while logical_end < len(segments) and segments[logical_end].cont:
                logical_end += 1
            logical_segments = segments[logical_start:logical_end]

            boundaries: list[tuple[int, int]] = []
            cursor = 0
            for segment in logical_segments:
                end = cursor + len(segment.source)
                boundaries.append((cursor, end))
                cursor = end

            metadata = logical_segments[0].meta.get("epub_annotations")
            raw_items = metadata.get("items") if isinstance(metadata, dict) else None
            items = raw_items if isinstance(raw_items, list) else []
            source_length = metadata.get("source_length") if isinstance(metadata, dict) else None
            if items and (
                not isinstance(source_length, int)
                or isinstance(source_length, bool)
                or source_length != cursor
            ):
                position = logical_end
                continue
            seen_by_piece: list[set[str]] = [set() for _ in logical_segments]

            for raw_item in items:
                if not isinstance(raw_item, dict) or raw_item.get("relation") != "noteref":
                    continue
                target_key = raw_item.get("target_key")
                if not isinstance(target_key, str) or not target_key:
                    continue
                record = raw_contexts.get(target_key)
                if not isinstance(record, dict):
                    continue
                raw_blocks = record.get("source_blocks")
                blocks = (
                    [block for block in raw_blocks if isinstance(block, str) and block.strip()]
                    if isinstance(raw_blocks, list)
                    else []
                )
                if not blocks:
                    continue
                note = {
                    "target_key": target_key,
                    "source": "\n\n".join(blocks),
                }

                start = raw_item.get("source_start")
                end = raw_item.get("source_end")
                if (
                    not isinstance(start, int)
                    or isinstance(start, bool)
                    or not isinstance(end, int)
                    or isinstance(end, bool)
                    or not 0 <= start <= end <= cursor
                ):
                    continue

                piece_indices: list[int]
                if raw_item.get("mode") == "range" and start < end:
                    piece_indices = [
                        index
                        for index, (piece_start, piece_end) in enumerate(boundaries)
                        if start < piece_end and end > piece_start
                    ]
                else:
                    # 边界上的 point 归前片；位置 0 归首片。
                    piece_index = 0
                    if start > 0:
                        piece_index = next(
                            (
                                index
                                for index, (_piece_start, piece_end) in enumerate(boundaries)
                                if start <= piece_end
                            ),
                            len(boundaries) - 1,
                        )
                    piece_indices = [piece_index]

                for piece_index in piece_indices:
                    if target_key in seen_by_piece[piece_index]:
                        continue
                    seen_by_piece[piece_index].add(target_key)
                    assigned[logical_start + piece_index].append(note)

            position = logical_end
        return assigned

    def align_segment_annotation(
        self,
        ci: int,
        chapter: Chapter,
        start_position: int,
        store: RunStore,
    ) -> None:
        """串行定位一个已译完逻辑原段的 EPUB 注释链接。

        超长段会被切成一个带 anchor 的首段和若干 cont 续段；解析元数据
        只存在首段，因此必须等全部续段都有译文后再合并 source/target。中文
        标点先在该逻辑段内定稿，保证 placement 的字符偏移不会在章末失效。

        定位结果无论正常还是确定性 fallback 都会立即写回章节文件。没有注释
        或译文尚不完整时直接返回，且不会调用模型。
        """
        segments = chapter.text_segments
        if not 0 <= start_position < len(segments):
            return
        while start_position > 0 and segments[start_position].cont:
            start_position -= 1
        segment = segments[start_position]
        metadata = segment.meta.get("epub_annotations")
        if not isinstance(metadata, dict):
            return
        raw_items = metadata.get("items")
        if not isinstance(raw_items, list) or not raw_items:
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
        expected_ids = {
            str(item.get("id")) for item in raw_items if isinstance(item, dict) and item.get("id")
        }
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

        items = tuple(dict(item) for item in raw_items if isinstance(item, dict))
        if not items:
            if target_changed:
                store.save_chapter(chapter)
            return
        anchor = segment.anchor or f"segment-{segment.index}"
        unit = AnnotationUnit(
            unit_id=f"ch{ci}:{anchor}",
            source=source,
            target=target,
            items=items,
        )
        if not self._runtime.config.pipeline.annotation_alignment:
            store.log_event(
                "annotation_alignment_skipped",
                chapter=ci,
                segment=segment.index,
                anchor=segment.anchor,
                unit_id=unit.unit_id,
                reason="disabled",
            )
            if target_changed:
                store.save_chapter(chapter)
            return

        try:
            result = self._runtime.annotation_aligner.align_unit(unit)
        except Exception as error:  # noqa: BLE001 - 单段失败由 writer 安全降级
            if target_changed:
                store.save_chapter(chapter)
            store.log_event(
                "annotation_alignment_failed",
                chapter=ci,
                segment=segment.index,
                anchor=segment.anchor,
                unit_id=unit.unit_id,
                error=type(error).__name__,
                detail=str(error),
            )
            return

        metadata["target_digest"] = result.target_digest
        metadata["placements"] = [dict(item) for item in result.placements]
        # 每个逻辑段完成后立即原子落盘；长书被中断时不必重新支付已完成的
        # 注释定位调用，也能在翻译尚未完成时导出查看当前效果。
        store.save_chapter(chapter)
        store.log_event(
            "annotation_alignment_completed",
            chapter=ci,
            segment=segment.index,
            anchor=segment.anchor,
            unit_id=unit.unit_id,
            annotations=len(items),
            used_fallback=result.used_fallback,
        )

    def align_annotations_after_batch(
        self,
        ci: int,
        chapter: Chapter,
        start: int,
        count: int,
        store: RunStore,
    ) -> None:
        """按原文顺序串行处理当前批次触及且已完整翻译的注释段。"""
        segments = chapter.text_segments
        for logical_start in self.completed_logical_starts_in_range(
            segments,
            start,
            count,
        ):
            self.align_segment_annotation(ci, chapter, logical_start, store)
