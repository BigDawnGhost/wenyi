"""HTML 读取器。

读取单文件 HTML（如 pandoc / calibre 转换的电子书），按标题层级切分为 Chapter，
再将每章内的块级元素切分为 Segment，并在元素上打 data-tn-id 占位标记，
供翻译后按标记回填译文。

与 epub_reader 保持完全一致的处理约定：
- 块级标签：p / h1-h6 / li / blockquote
- 嵌套块跳过（如 li 内的 p），避免重复
- 标题类标签（h1-h6）kind=heading，其余 kind=text
- 占位锚点格式：tn{章序号}_{段序号}
- 每章的 template 字段存储该章打完标记后的完整 HTML 片段

章节切分：
  参考 text_reader（所有级别标题均断章），默认 h1-h6 都作为章起点。
  每遇到一个标题就新开一章。可通过 chapter_tags 参数自定义。
"""

from __future__ import annotations

import os

from bs4 import BeautifulSoup, Tag

from .models import KIND_HEADING, KIND_TEXT, Chapter, Document, Segment

# 块级 / 标题标签集合（与 epub_reader 一致）
_BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote"}
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

# 默认所有标题级别都作为章起点（参考 text_reader 的 #{1,3}）
_DEFAULT_CHAPTER_TAGS: frozenset[str] = frozenset(_HEADING_TAGS)


# ── 内部辅助 ─────────────────────────────────────────────


def _extract_chapter(
    html: str,
    chapter_index: int,
    *,
    chapter_title: str = "",
) -> tuple[str, list[Segment], str]:
    """解析章节 HTML 片段，返回 (标题, segments, 带标记的模板 HTML)。

    与 epub_reader.annotate_epub_resource 的块级提取逻辑一致：
    1. 遍历所有块级元素，跳过嵌套子块
    2. 为每个有效块打 data-tn-id 标记
    3. 标题优先用显式传入的 chapter_title，其次用首个 heading segment
    """
    soup = BeautifulSoup(html, "html.parser")
    segments: list[Segment] = []
    idx = 0
    for el in soup.find_all(_BLOCK_TAGS):
        # 跳过嵌套在另一个块级元素内的块（如 blockquote / li 里的 p）
        if any(getattr(p, "name", None) in _BLOCK_TAGS for p in el.parents):
            continue
        text = el.get_text().strip()
        if not text:
            continue
        anchor = f"tn{chapter_index}_{idx}"
        el["data-tn-id"] = anchor
        kind = KIND_HEADING if el.name in _HEADING_TAGS else KIND_TEXT
        segments.append(Segment(index=idx, source=text, kind=kind, anchor=anchor))
        idx += 1

    # 标题优先级：显式传入 > 首个 heading segment > 无标题
    title = chapter_title.strip()
    if not title:
        for s in segments:
            if s.kind == KIND_HEADING:
                title = s.source
                break

    return title, segments, str(soup)


# ── 公开 API ──────────────────────────────────────────────


def read_html(
    path: str,
    source_lang: str,
    target_lang: str,
    *,
    chapter_tags: frozenset[str] | None = _DEFAULT_CHAPTER_TAGS,
    encoding: str = "utf-8",
) -> Document:
    """解析单个 HTML 文件，返回 Document。

    章节切分规则：
    1. body 中每个标题（默认 h1-h6）新开一章，连续标题合并为同一章；
    2. 第一个标题之前的内容作为「前页」章（若含正文块）；
    3. chapter_tags 为 None 时整篇作为一章；
    4. 各章内部块级元素（p / h1-h6 / li / blockquote）切为 Segment，
       嵌套块自动跳过，与 epub_reader 行为一致。

    Parameters
    ----------
    path : str
        HTML 文件路径。
    source_lang : str
        源语言代码。
    target_lang : str
        目标语言代码。
    chapter_tags : frozenset[str] | None
        作为章起点的 HTML 标签集合，默认 h1-h6。传入 None 整篇一章。
    encoding : str
        文件编码，默认 utf-8。
    """
    with open(path, "r", encoding=encoding, errors="replace") as f:
        soup = BeautifulSoup(f, "html.parser")

    # 书名直接从文件名提取（与 text_reader 对齐）
    book_title = os.path.splitext(os.path.basename(path))[0]

    # 只保存 <head> 的内容，导出时由 writer 统一创建外层 <head>。
    head_html = soup.head.decode_contents() if soup.head else ""

    body = soup.body if soup.body else soup

    # 收集 body 的直接子元素（Tag + NavigableString），按文档顺序
    children: list = list(body.children)

    # 找到章节边界：连续标题只取第一个作为章起点（其余合并入同一章）
    boundaries: list[int] = []
    if chapter_tags:
        for i, child in enumerate(children):
            if isinstance(child, Tag) and child.name in chapter_tags:
                # 检查前一个非空白元素是否也是标题
                prev_is_heading = False
                for j in range(i - 1, -1, -1):
                    prev = children[j]
                    if isinstance(prev, Tag):
                        prev_is_heading = prev.name in chapter_tags
                        break
                    if hasattr(prev, "strip") and prev.strip():
                        break  # 非空文本节点 → 标题不连续
                if not prev_is_heading:
                    boundaries.append(i)

    # 构建 (start, end) 区间
    if not boundaries:
        intervals: list[tuple[int, int]] = [(0, len(children))]
    else:
        intervals = []
        # 第一个标题之前的前页
        if boundaries[0] > 0:
            intervals.append((0, boundaries[0]))
        # 各章：每遇到标题就断章
        for bi, start in enumerate(boundaries):
            end = boundaries[bi + 1] if bi + 1 < len(boundaries) else len(children)
            intervals.append((start, end))

    chapters: list[Chapter] = []
    ci = 0
    for start, end in intervals:
        ch_children = children[start:end]
        fragment_html = "".join(str(c) for c in ch_children)

        # 拼接所有连续标题作为章节标题（用 / 分隔，去换行；跳过空白）
        chapter_title = ""
        first = ch_children[0] if ch_children else None
        if isinstance(first, Tag) and first.name in _HEADING_TAGS:
            titles = []
            for c in ch_children:
                if isinstance(c, Tag) and c.name in _HEADING_TAGS:
                    t = " ".join(c.get_text().split())  # 去换行归一化
                    titles.append(t)
                elif isinstance(c, Tag):
                    break  # 非标题 Tag → 停止
                elif hasattr(c, "strip") and c.strip():
                    break  # 非空文本节点 → 停止
                # 纯空白 NavigableString → 跳过，继续
            chapter_title = " / ".join(titles)

        title, segments, template = _extract_chapter(
            fragment_html,
            ci,
            chapter_title=chapter_title,
        )

        if not any(s.source.strip() for s in segments):
            continue

        chapters.append(
            Chapter(
                index=ci,
                title=title,
                segments=segments,
                href=None,
                template=template,
            )
        )
        ci += 1

    return Document(
        title=book_title,
        source_lang=source_lang,
        target_lang=target_lang,
        fmt="html",
        source_path=os.path.abspath(path),
        chapters=chapters,
        meta={
            "chapter_tags": list(chapter_tags) if chapter_tags else None,
            "head_html": head_html,
        },
    )
