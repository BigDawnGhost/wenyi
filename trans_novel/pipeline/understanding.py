"""旧版流水线到应用层全书理解协调器的适配。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from ..application.understanding import UnderstandingChapter, build_understanding
from .runstore import RunStore

ProgressFn = Callable[[int, int, str], None]


def build_legacy_understanding(
    store: RunStore,
    *,
    enabled: bool,
    concurrency: int,
    digest_chapter: Callable[[str], str],
    summarize_book: Callable[[list[str], str], str],
    style_brief: Callable[[dict[str, Any]], str],
    progress: ProgressFn | None = None,
) -> str:
    """把旧 ``RunStore`` 和 Agent 回调接到纯协调服务。"""
    if not enabled:
        # 保持禁用路径为真正的短路，不读取 manifest 或章节状态。
        return build_understanding(
            (),
            synopsis_order=(),
            enabled=False,
            concurrency=concurrency,
            digest_chapter=digest_chapter,
            summarize_book=summarize_book,
            style_brief=style_brief,
            load_analysis=store.load_analysis,
            save_analysis=store.save_analysis,
            save_digest=lambda _chapter, _digest: None,
            emit_event=_legacy_event_sink(store),
            progress=progress,
        )

    manifest = store.load_manifest()
    manifest_chapters = manifest.get("chapters", [])
    loaded = {
        chapter.get("index", position): store.load_chapter(chapter.get("index", position))
        for position, chapter in enumerate(manifest_chapters)
    }
    chapters = tuple(
        UnderstandingChapter(
            index=chapter_index,
            source_text="\n".join(segment.source for segment in chapter.text_segments),
            source_digest=chapter.meta.get("source_digest", "") or "",
        )
        for chapter_index, chapter in loaded.items()
    )
    synopsis_order = tuple(
        chapter.get("index", position) for position, chapter in enumerate(manifest_chapters)
    )

    def save_digest(chapter_index: int, digest: str) -> None:
        chapter = loaded[chapter_index]
        chapter.meta["source_digest"] = digest
        store.save_chapter(chapter)

    return build_understanding(
        chapters,
        synopsis_order=synopsis_order,
        enabled=True,
        concurrency=concurrency,
        digest_chapter=digest_chapter,
        summarize_book=summarize_book,
        style_brief=style_brief,
        load_analysis=store.load_analysis,
        save_analysis=store.save_analysis,
        save_digest=save_digest,
        emit_event=_legacy_event_sink(store),
        progress=progress,
    )


def _legacy_event_sink(store: RunStore) -> Callable[[str, Mapping[str, object]], None]:
    """把不可变应用事件负载还原为旧版关键字事件接口。"""

    def emit(event: str, attributes: Mapping[str, object]) -> None:
        store.log_event(event, **attributes)

    return emit


__all__ = ["build_legacy_understanding"]
