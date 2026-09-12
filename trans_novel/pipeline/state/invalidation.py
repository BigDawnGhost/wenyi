"""Pure state and artifact mutations used by :mod:`runstore`."""

from __future__ import annotations

from trans_novel.pipeline.state.models import STATUS_PENDING, ChapterProgress


def reconcile_fingerprints(state, computed: dict[str, str]) -> set[str]:
    """Invalidate mismatched nodes and their descendants in ``state``."""
    return state.reconcile_fingerprints(computed)


def clear_translation_targets(chapter, state, ci: int) -> None:
    """Clear translated chapter artifacts and reset its progress."""
    for segment in chapter.text_segments:
        segment.reset_translation()
    state.progress.setdefault(ci, ChapterProgress())
    progress = state.progress[ci]
    progress.status = STATUS_PENDING
    progress.pending_polish = []
    progress.lint_issues = []
    state.progress[ci] = progress


def clear_translated_titles(state) -> None:
    """Clear translated chapter and TOC titles from the manifest state."""
    for chapter in state.chapters:
        chapter.title_translated = None
    raw_toc = state.meta.get("toc_entries") if isinstance(state.meta, dict) else None
    if isinstance(raw_toc, list):
        for entry in raw_toc:
            if isinstance(entry, dict):
                entry.pop("title_translated", None)


__all__ = [
    "clear_translated_titles",
    "clear_translation_targets",
    "reconcile_fingerprints",
]
