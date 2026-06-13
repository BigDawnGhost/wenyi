"""文档加载分发 + 翻译批次切分。

- load_document：按扩展名分发到 EPUB / 纯文本读取器。
- batch_segments：把一章的 Segment 按字符预算（≈token）打包成批次，
  一个批次整体发给翻译模型；模型须返回等长译文数组以做对齐校验。
  单个超长 Segment 自成一批（不物理拆分 Segment，保证回填一一对应）。
"""

from __future__ import annotations

import os

from .models import Chapter, Document, Segment
from .epub_reader import read_epub
from .text_reader import read_text


def load_document(path: str, source_lang: str, target_lang: str) -> Document:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".epub":
        return read_epub(path, source_lang, target_lang)
    if ext in (".txt", ".md", ".markdown", ".text"):
        return read_text(path, source_lang, target_lang)
    raise ValueError(f"不支持的格式：{ext}（支持 .epub / .txt / .md）")


def batch_segments(segments: list[Segment], max_chars: int) -> list[list[Segment]]:
    """把 Segment 列表按字符预算分批。"""
    batches: list[list[Segment]] = []
    cur: list[Segment] = []
    cur_len = 0
    for s in segments:
        slen = len(s.source)
        if cur and cur_len + slen > max_chars:
            batches.append(cur)
            cur, cur_len = [], 0
        cur.append(s)
        cur_len += slen
    if cur:
        batches.append(cur)
    return batches


def chapter_batches(chapter: Chapter, max_chars: int) -> list[list[Segment]]:
    """对一章的可翻译 Segment 分批。"""
    return batch_segments(chapter.text_segments, max_chars)
