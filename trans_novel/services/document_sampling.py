"""从只读文档视图确定性提取语言检测或全书风格分析样本。

采样服务不依赖 Pydantic ``Document``、RunStore、LLM 或图运行时，也不保留
调用方正文对象。它只要求 Document → Chapter → text_segments → source 的
最小结构，因此旧编排器和未来 Artifact codec 可以共享同一采样规则。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

_LONG_CHAPTER_THRESHOLD = 200
_MAX_PLAIN_CHARS = 6000
_LABELED_CHUNK_CHARS = 2800


class SourceSegmentView(Protocol):
    """文档采样所需的最小段落接口。"""

    @property
    def source(self) -> str:
        """返回当前段落的源文。"""
        ...


class SourceChapterView(Protocol):
    """文档采样所需的最小章节接口。"""

    @property
    def text_segments(self) -> Sequence[SourceSegmentView]:
        """返回按正文顺序排列的可采样段落。"""
        ...


class SourceDocumentView(Protocol):
    """文档采样所需的最小整书接口。"""

    @property
    def chapters(self) -> Sequence[SourceChapterView]:
        """返回按书内顺序排列的章节。"""
        ...


def sample_document_text(
    document: SourceDocumentView,
    *,
    labeled: bool = True,
) -> str:
    """返回稳定的多点风格样本或无标签语言检测样本。

    ``labeled=True`` 时，从足够长章节的开头、中部和结尾各取一块并去重；
    ``False`` 时只返回第一篇足够长章节的纯源文。若全书都是短章，两种模式
    都回退到前两章拼接，且不注入中文标签。
    """
    if type(labeled) is not bool:
        raise TypeError("labeled 必须是布尔值")
    chapter_texts, short_book_sources = _copy_document_text(document)
    long_texts = [text for text in chapter_texts if len(text) > _LONG_CHAPTER_THRESHOLD]

    # 旧实现按段落平铺前两章；保留该细节可避免空章节额外注入换行。
    if not long_texts:
        return "\n".join(short_book_sources)[:_MAX_PLAIN_CHARS]
    if not labeled:
        return long_texts[0][:_MAX_PLAIN_CHARS]

    picks = (
        (0, "开头样章"),
        (len(long_texts) // 2, "中部样章"),
        (len(long_texts) - 1, "结尾样章"),
    )
    parts: list[str] = []
    seen: set[int] = set()
    for index, label in picks:
        if index in seen:
            continue
        seen.add(index)
        text = long_texts[index]
        chunk = text[-_LABELED_CHUNK_CHARS:] if label == "结尾样章" else text[:_LABELED_CHUNK_CHARS]
        parts.append(f"【{label}】\n{chunk}")
    return "\n\n".join(parts)


def _copy_document_text(document: SourceDocumentView) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """校验并复制章级文本及旧短书回退所需的前两章段落。"""
    chapters = getattr(document, "chapters", None)
    if isinstance(chapters, (str, bytes)) or not isinstance(chapters, Sequence):
        raise TypeError("document.chapters 必须是章节序列")

    chapter_texts: list[str] = []
    short_book_sources: list[str] = []
    for chapter_index, chapter in enumerate(chapters):
        segments = getattr(chapter, "text_segments", None)
        if isinstance(segments, (str, bytes)) or not isinstance(segments, Sequence):
            raise TypeError(f"document.chapters[{chapter_index}].text_segments 必须是段落序列")
        sources: list[str] = []
        for segment_index, segment in enumerate(segments):
            source = getattr(segment, "source", None)
            if type(source) is not str:
                raise TypeError(
                    "document.chapters"
                    f"[{chapter_index}].text_segments[{segment_index}].source 必须是字符串"
                )
            sources.append(source)
        chapter_texts.append("\n".join(sources))
        if chapter_index < 2:
            short_book_sources.extend(sources)
    return tuple(chapter_texts), tuple(short_book_sources)


__all__ = [
    "SourceChapterView",
    "SourceDocumentView",
    "SourceSegmentView",
    "sample_document_text",
]
