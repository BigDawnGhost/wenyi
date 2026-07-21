"""EPUB 逻辑章节切分策略。

切章策略只决定哪些目录节点是章边界，不负责解析 XHTML。这个边界
使默认的“最高层目录切章”以后可替换为按标题级别、用户选择或
启发式策略，而无需重写目录解析和物理资源回填。
"""

from __future__ import annotations

from typing import Any, Protocol


class ChapterSplitStrategy(Protocol):
    """逻辑章节边界选择器接口。"""

    name: str

    def select(self, toc_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """从已定位的目录节点中返回章边界。"""
        ...


class TopLevelTocStrategy:
    """仅使用目录中 ``depth == 0`` 的可定位节点切章。"""

    name = "top-level-toc"

    def select(self, toc_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """选出顶层、内部且已关联到 Segment 的节点，并去掉重复边界。"""
        by_position: dict[int, dict[str, Any]] = {}
        for entry in toc_entries:
            position = entry.get("boundary_position")
            if (
                entry.get("depth") != 0
                or entry.get("external")
                or not isinstance(position, int)
                or position < 0
            ):
                continue
            previous = by_position.get(position)
            if previous is None:
                by_position[position] = entry
                continue
            previous_is_anchored = bool(previous.get("segment_anchor"))
            current_is_anchored = bool(entry.get("segment_anchor"))
            if current_is_anchored and not previous_is_anchored:
                # 空标题页和下一个真实章节可以落在同一扁平位置。
                # 此时必须优先有 Segment 锚点的真实章节。
                by_position[position] = entry
            elif not current_is_anchored and not previous_is_anchored:
                # 连续空资源都无正文可分，让更接近后续正文的节点命名。
                by_position[position] = entry
        return list(by_position.values())


_STRATEGIES: dict[str, ChapterSplitStrategy] = {
    TopLevelTocStrategy.name: TopLevelTocStrategy(),
}


def get_chapter_split_strategy(name: str = TopLevelTocStrategy.name) -> ChapterSplitStrategy:
    """按稳定名称返回切章策略，为后续扩展保留单一入口。"""
    try:
        return _STRATEGIES[name]
    except KeyError as error:
        raise ValueError(f"Unknown EPUB chapter split strategy: {name}") from error
