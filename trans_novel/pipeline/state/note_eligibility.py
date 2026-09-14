"""检查旧版已完成任务是否允许迁移注释槽位。"""

from __future__ import annotations

from trans_novel.ingest import Chapter
from trans_novel.pipeline.state.models import (
    BEST_EFFORT_NODES,
    NODE_ANALYZE,
    NODE_DETERMINISTIC_QA,
    NODE_FAILED_PERMANENT,
    NODE_FAILED_RETRYABLE,
    NODE_POLISH,
    NODE_PREPARE,
    NODE_REPAIR,
    NODE_SKIPPED,
    NODE_SUCCEEDED,
    NODE_TITLES,
    NODE_TRANSLATE,
    STATUS_DONE,
    RunState,
    chapter_node_key,
)


def completed_legacy_note_content(state: RunState, chapters: list[Chapter]) -> bool:
    """复用旧版输出维护对内容完成状态的判断。"""
    by_index = {chapter.index: chapter for chapter in chapters}
    for item in state.chapters:
        progress = state.progress.get(item.index)
        chapter = by_index.get(item.index)
        if (
            progress is None
            or progress.status != STATUS_DONE
            or progress.pending_polish
            or chapter is None
            or any(not (segment.target or "").strip() for segment in chapter.text_segments)
        ):
            return False
    for node_id in (
        NODE_PREPARE,
        NODE_ANALYZE,
        NODE_TITLES,
        NODE_DETERMINISTIC_QA,
        NODE_REPAIR,
    ):
        node = state.nodes.get(node_id)
        if node is None or node.status != NODE_SUCCEEDED:
            return False
    for item in state.chapters:
        if state.nodes.get(chapter_node_key(NODE_TRANSLATE, item.index), None) is None:
            return False
        if state.nodes[chapter_node_key(NODE_TRANSLATE, item.index)].status != NODE_SUCCEEDED:
            return False
        preserved = item.processing is not None and item.processing.action == "preserve"
        progress = state.progress[item.index]
        legacy_bypass = item.processing is None and progress.back_matter_mode is not None
        if not preserved and not legacy_bypass:
            polish = state.nodes.get(chapter_node_key(NODE_POLISH, item.index))
            if polish is None or polish.status not in (NODE_SUCCEEDED, NODE_SKIPPED):
                return False
    ignored_failures = {*BEST_EFFORT_NODES, "layout", "report", "assemble"}
    return not any(
        node.status in (NODE_FAILED_RETRYABLE, NODE_FAILED_PERMANENT)
        and key.partition(":")[0] not in ignored_failures
        for key, node in state.nodes.items()
    )


__all__ = ["completed_legacy_note_content"]
