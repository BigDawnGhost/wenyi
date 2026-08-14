"""全书理解阶段的框架无关协调逻辑。

本模块只编排章节摘要计算和结果提交，不依赖旧版 ``RunStore``、具体 Agent、
配置模型或文件系统。旧流水线通过窄回调把这些基础设施能力接入。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

ProgressFn = Callable[[int, int, str], None]
EventFn = Callable[[str, Mapping[str, object]], None]


@dataclass(frozen=True, slots=True)
class UnderstandingChapter:
    """协调层所需的最小章节快照。"""

    index: int
    source_text: str
    source_digest: str = ""


def build_understanding(
    chapters: Sequence[UnderstandingChapter],
    *,
    synopsis_order: Sequence[int],
    enabled: bool,
    concurrency: int,
    digest_chapter: Callable[[str], str],
    summarize_book: Callable[[list[str], str], str],
    style_brief: Callable[[dict[str, Any]], str],
    load_analysis: Callable[[], dict[str, Any] | None],
    save_analysis: Callable[[dict[str, Any]], None],
    save_digest: Callable[[int, str], None],
    emit_event: EventFn,
    progress: ProgressFn | None = None,
) -> str:
    """生成并保存逐章梗概和全书概览，返回可注入翻译提示的概览。

    章节 LLM 调用在线程池中并发执行。保存、事件和进度回调仍由调用线程按
    future 完成顺序执行，保持旧版可观察行为并避免存储写竞争；最终全书概览
    则显式按 manifest 提供的 ``synopsis_order`` 组装，隔离并发完成顺序。
    """
    if not enabled:
        emit_event("book_understanding_skipped", {"reason": "disabled"})
        return ""

    worker_count = max(1, concurrency)
    digests_by_chapter = {chapter.index: chapter.source_digest for chapter in chapters}
    pending = [chapter for chapter in chapters if not chapter.source_digest]
    if pending:
        emit_event(
            "book_understanding_chapter_digest_started",
            {
                "chapters": [chapter.index for chapter in pending],
                "workers": worker_count,
            },
        )
        if progress:
            progress(0, len(pending), "预扫章节梗概")

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(digest_chapter, chapter.source_text): chapter.index
                for chapter in pending
            }
            for completed, future in enumerate(as_completed(futures), start=1):
                chapter_index = futures[future]
                digest = future.result()
                digests_by_chapter[chapter_index] = digest
                save_digest(chapter_index, digest)
                emit_event(
                    "book_understanding_chapter_digest_saved",
                    {"chapter": chapter_index, "digest": digest},
                )
                if progress:
                    progress(completed, len(pending), "预扫章节梗概")

    digests = [digests_by_chapter[chapter_index] or "" for chapter_index in synopsis_order]
    analysis = load_analysis() or {}
    synopsis = analysis.get("book_synopsis", "")
    if not synopsis and any(digest.strip() for digest in digests):
        if progress:
            progress(0, 0, "生成全书概览…")
        synopsis = summarize_book(digests, style_brief(analysis))
        analysis["book_synopsis"] = synopsis
        save_analysis(analysis)
        emit_event("book_synopsis_saved", {"synopsis": synopsis})
    return synopsis


__all__ = ["UnderstandingChapter", "build_understanding"]
