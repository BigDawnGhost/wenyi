"""EPUB logical chapter boundary policies.
Policies select TOC chapter boundaries without parsing XHTML. This separates the default
top-level TOC policy from future heading-level, user-selected or heuristic policies, which
can reuse TOC parsing and physical-resource backfill.
"""

from __future__ import annotations

from typing import Any, Protocol


class ChapterSplitStrategy(Protocol):
    """Interface for selecting logical chapter boundaries."""

    name: str

    def select(self, toc_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return chapter boundaries from located TOC nodes."""
        ...


class TopLevelTocStrategy:
    """Split only at resolvable TOC nodes with depth == 0."""

    name = "top-level-toc"

    def select(self, toc_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Select top-level internal nodes linked to segments and remove duplicate boundaries."""
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
                # An empty title page and the next real chapter may share a flat position.
                # Prefer the real chapter with a Segment anchor in that case.
                by_position[position] = entry
            elif not current_is_anchored and not previous_is_anchored:
                # For consecutive empty resources, use the node closest to the following body text.
                by_position[position] = entry
        return list(by_position.values())


_STRATEGIES: dict[str, ChapterSplitStrategy] = {
    TopLevelTocStrategy.name: TopLevelTocStrategy(),
}


def get_chapter_split_strategy(
    name: str = TopLevelTocStrategy.name,
) -> ChapterSplitStrategy:
    """Resolve a chapter policy by stable name through one extensible entry point."""
    try:
        return _STRATEGIES[name]
    except KeyError as error:
        raise ValueError(f"Unknown EPUB chapter split strategy: {name}") from error
