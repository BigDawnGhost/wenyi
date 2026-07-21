"""文档加载分发 + 翻译批次切分。

- load_document：按扩展名分发到 EPUB / FB2 / TXT / Markdown / HTML / PDF
  读取器；可选把超长 Segment 按句拆分。
- batch_segments：把一章的 Segment 按字符预算（≈token）打包成批次，
  一个批次整体发给翻译模型；模型须返回等长译文数组以做对齐校验。
- split_long_segments：单个 Segment 超过 max_chars 时按句切成多段（续段标 cont=True），
  回填时由 writer 把续段并回同一段落/同一 EPUB 元素，保持结构一一对应。
"""

from __future__ import annotations

from copy import deepcopy
import os
import re

from .epub_reader import read_epub
from .fb2_reader import read_fb2
from .html_reader import read_html
from .models import KIND_TEXT, Chapter, Document, Segment
from .pdf_reader import read_pdf
from .text_reader import read_text

# 常见句末标点，用于超长段的按句拆分
_SENT_SPLIT = re.compile(r"(?<=[。．.!！？!?…\n])")


def _split_oversized_sentence(text: str, max_chars: int) -> list[str]:
    """兜底拆分单个超长句：优先不拆英文单词，找不到空白才硬切。"""
    chunks: list[str] = []
    rest = text
    while len(rest) > max_chars:
        cut = rest.rfind(" ", 0, max_chars + 1)
        if cut <= 0:
            cut = rest.rfind("\t", 0, max_chars + 1)
        if cut <= 0:
            cut = rest.rfind("\n", 0, max_chars + 1)
        if cut <= 0:
            cut = max_chars
        chunks.append(rest[:cut])
        rest = rest[cut:]
    if rest:
        chunks.append(rest)
    return chunks


def _split_text(text: str, max_chars: int) -> list[str]:
    """把超长文本按句末标点贪心打包；单句过长才按空白兜底拆。"""
    chunks: list[str] = []
    cur = ""
    for p in _SENT_SPLIT.split(text):
        if not p:
            continue
        if len(p) > max_chars:  # 单句本身超长 → 兜底拆
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.extend(_split_oversized_sentence(p, max_chars))
            continue
        if cur and len(cur) + len(p) > max_chars:
            chunks.append(cur)
            cur = ""
        cur += p
    if cur:
        chunks.append(cur)
    return chunks or [text]


def split_long_segments(chapters: list[Chapter], max_chars: int) -> None:
    """就地把各章里超过 max_chars 的 Segment 拆成多段；续段 cont=True、不带 anchor。"""
    if not max_chars or max_chars <= 0:
        return
    for ch in chapters:
        new_segs: list[Segment] = []
        idx = 0
        for s in ch.segments:
            if len(s.source) <= max_chars:
                s.index = idx
                new_segs.append(s)
                idx += 1
                continue
            for k, piece in enumerate(_split_text(s.source, max_chars)):
                if k == 0:
                    new_segs.append(
                        Segment(
                            index=idx,
                            source=piece,
                            kind=s.kind,
                            anchor=s.anchor,
                            resource_href=s.resource_href,
                            cont=False,
                            meta=deepcopy(s.meta),
                        )
                    )
                else:  # 续段：并回首段，无独立 anchor
                    new_segs.append(
                        Segment(
                            index=idx,
                            source=piece,
                            kind=KIND_TEXT,
                            anchor=None,
                            resource_href=s.resource_href,
                            cont=True,
                        )
                    )
                idx += 1
        ch.segments = new_segs


def load_document(
    path: str,
    source_lang: str,
    target_lang: str,
    split_segments: int = 0,
    *,
    cache_dir: str | None = None,
) -> Document:
    """按文件扩展名读取文档，并按需拆分超过上限的翻译段。"""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".epub":
        doc = read_epub(path, source_lang, target_lang)
    elif ext in (".md", ".markdown", ".txt", ".text"):
        doc = read_text(path, source_lang, target_lang)
    elif ext == ".fb2":
        doc = read_fb2(path, source_lang, target_lang)
    elif ext in (".html", ".htm"):
        doc = read_html(path, source_lang, target_lang)
    elif ext == ".pdf":
        if cache_dir is None:
            raise ValueError("PDF 读取需要指定运行状态缓存目录")
        doc = read_pdf(
            path,
            source_lang,
            target_lang,
            cache_dir=cache_dir,
        )
    else:
        raise ValueError(
            f"不支持的格式：{ext}（支持 .epub / .txt / .md / .fb2 / .html / .pdf）"
        )

    if split_segments and split_segments > 0:
        split_long_segments(doc.chapters, split_segments)
    return doc


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
