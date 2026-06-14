"""回填：把译文写回原格式。

- 纯文本：按章重建，标题 + 段落（空行分隔）。
- EPUB：重开原始 zip，逐条目原样拷贝；命中章节 href 的 XHTML 用 chapter.template
  按 data-tn-id 锚点替换为译文后写回，非正文资源（图片/CSS/字体）不动。
缺失译文的段回退使用原文，保证不丢内容。
"""

from __future__ import annotations

import os
import re
import zipfile

from bs4 import BeautifulSoup

from ..ingest.models import KIND_HEADING, Chapter
from ..pipeline.runstore import RunStore

_ILLEGAL_FN = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def _sanitize_filename(name: str, fallback: str = "translated") -> str:
    name = _ILLEGAL_FN.sub(" ", name or "").strip().strip(".")
    name = re.sub(r"\s+", " ", name)
    return name[:120] or fallback


def _default_out(source_path: str, out_format: str, title: str | None = None) -> str:
    ext = ".epub" if out_format == "epub" else ".txt"
    if title and title.strip():
        # 用译名作输出文件名，落在源文件同目录
        d = os.path.dirname(os.path.abspath(source_path))
        return os.path.join(d, _sanitize_filename(title) + ext)
    base, _ = os.path.splitext(source_path)
    return f"{base}.zh{ext}"


def _ch_title(c: dict) -> str:
    """章节展示标题：优先译名，回退原标题。"""
    return (c.get("title_translated") or c.get("title") or "").strip()


def _seg_text(seg) -> str:
    return seg.target if (seg.target and seg.target.strip()) else seg.source


def _merged_paragraphs(chapter: Chapter) -> list[tuple[str, str]]:
    """把章内 Segment 合并为段落，cont 续段并回上一段。返回 [(kind, text), ...]。"""
    paras: list[list[str]] = []      # 每段累积的文本片段
    kinds: list[str] = []
    for s in chapter.segments:
        if not s.source.strip():
            continue
        if s.cont and paras:
            paras[-1].append(_seg_text(s))
        else:
            paras.append([_seg_text(s)])
            kinds.append(s.kind)
    return [(k, "".join(p)) for k, p in zip(kinds, paras)]


# ── 纯文本 ──────────────────────────────────────────────────────────────────
def _assemble_text(store: RunStore, out_path: str) -> str:
    m = store.load_manifest()
    chapter_blocks: list[str] = []
    for c in m["chapters"]:
        ch = store.load_chapter(c["index"])
        paras = [text for _, text in _merged_paragraphs(ch)]
        chapter_blocks.append("\n\n".join(paras))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n\n".join(chapter_blocks) + "\n")
    return out_path


# ── EPUB ────────────────────────────────────────────────────────────────────
def _render_chapter_html(chapter: Chapter) -> str:
    soup = BeautifulSoup(chapter.template or "", "html.parser")
    # 合并 cont 续段：续段文本并回其所属 anchor 元素
    by_anchor: dict[str, str] = {}
    cur_anchor: str | None = None
    for s in chapter.segments:
        if s.cont and cur_anchor is not None:
            by_anchor[cur_anchor] += _seg_text(s)
        elif s.anchor:
            cur_anchor = s.anchor
            by_anchor[cur_anchor] = _seg_text(s)
    for anchor, text in by_anchor.items():
        el = soup.find(attrs={"data-tn-id": anchor})
        if el is None:
            continue
        el.clear()
        el.append(text)
        del el["data-tn-id"]
    return str(soup)


def _base_no_frag(href: str) -> str:
    """取 href 的文件名（去目录、去 #锚点），用于跨文件相对路径匹配。"""
    return os.path.basename((href or "").split("#", 1)[0])


def _rewrite_opf_title(data: bytes, book_title: str) -> bytes:
    """把 OPF 里的 dc:title 改为译名（仅首个主标题）。失败则原样返回。"""
    if not book_title:
        return data
    try:
        soup = BeautifulSoup(data, "xml")
        el = soup.find("dc:title") or soup.find("title")
        if el is not None:
            el.clear()
            el.append(book_title)
            return soup.encode()
    except Exception:
        pass
    return data


def _rewrite_toc(data: bytes, title_by_base: dict[str, str], *, is_ncx: bool) -> bytes:
    """把目录（NCX navLabel / NAV 的 <a>）标题文本改为译名，按 href 文件名匹配。"""
    try:
        if is_ncx:
            soup = BeautifulSoup(data, "xml")
            for np in soup.find_all("navPoint"):
                content = np.find("content")
                label = np.find("text")
                if content is None or label is None:
                    continue
                t = title_by_base.get(_base_no_frag(content.get("src", "")))
                if t:
                    label.clear()
                    label.append(t)
            return soup.encode()
        # EPUB3 nav.xhtml：只改 epub:type="toc" 的导航，避免误改 landmarks / page-list
        soup = BeautifulSoup(data, "html.parser")
        toc_navs = [n for n in soup.find_all("nav")
                    if "toc" in (n.get("epub:type", "") or n.get("type", "")).split()]
        scopes = toc_navs or [soup]  # 找不到带类型的 toc nav 时退回全局
        for scope in scopes:
            for a in scope.find_all("a", href=True):
                t = title_by_base.get(_base_no_frag(a["href"]))
                if t:
                    a.clear()
                    a.append(t)
        return str(soup).encode("utf-8")
    except Exception:
        return data


def _assemble_epub(store: RunStore, source_path: str, out_path: str) -> str:
    m = store.load_manifest()
    # href -> 渲染后的 XHTML
    rendered: dict[str, str] = {}
    for c in m["chapters"]:
        ch = store.load_chapter(c["index"])
        if ch.href and ch.template:
            rendered[ch.href] = _render_chapter_html(ch)

    # 目录标题映射（文件名 → 译名），书名译名
    title_by_base: dict[str, str] = {}
    for c in m["chapters"]:
        base = _base_no_frag(c.get("href") or "")
        t = _ch_title(c)
        if base and t:
            title_by_base[base] = t
    book_title = (m.get("title_translated") or "").strip()

    with zipfile.ZipFile(source_path, "r") as zin:
        infos = zin.infolist()
        with zipfile.ZipFile(out_path, "w") as zout:
            for info in infos:
                name = info.filename
                low = name.lower()
                if name in rendered:
                    zout.writestr(info, rendered[name].encode("utf-8"))
                elif name == "mimetype":
                    zout.writestr(info, zin.read(name), zipfile.ZIP_STORED)
                elif low.endswith(".opf"):
                    zout.writestr(info, _rewrite_opf_title(zin.read(name), book_title))
                elif low.endswith(".ncx"):
                    zout.writestr(info, _rewrite_toc(zin.read(name), title_by_base, is_ncx=True))
                elif low.endswith((".xhtml", ".html", ".htm")) and _is_nav(zin.read(name)):
                    zout.writestr(info, _rewrite_toc(zin.read(name), title_by_base, is_ncx=False))
                else:
                    zout.writestr(info, zin.read(name))
    return out_path


def _is_nav(data: bytes) -> bool:
    return b"epub:type" in data and b"toc" in data


def _build_epub_from_chapters(store: RunStore, out_path: str) -> str:
    """从章节数据生成一个规范的 EPUB3（用于纯文本输入），使用 ebooklib。"""
    from html import escape

    from ebooklib import epub

    m = store.load_manifest()
    title = m.get("title_translated") or m.get("title", "translated")
    lang = m.get("target_lang", "zh")

    book = epub.EpubBook()
    book.set_identifier(f"trans-novel-{title}")
    book.set_title(title)
    book.set_language(lang)

    spine: list = ["nav"]
    toc: list = []
    for c in m["chapters"]:
        ch = store.load_chapter(c["index"])
        ch_title = _ch_title(c) or ch.title
        body_parts = []
        for kind, text in _merged_paragraphs(ch):
            tag = "h1" if kind == KIND_HEADING else "p"
            body_parts.append(f"<{tag}>{escape(text)}</{tag}>")
        fname = f"ch{c['index']}.xhtml"
        item = epub.EpubHtml(title=ch_title, file_name=fname, lang=lang)
        item.content = (
            f'<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{lang}">'
            f"<head><title>{escape(ch_title)}</title></head>"
            f"<body>{''.join(body_parts)}</body></html>"
        )
        book.add_item(item)
        spine.append(item)
        toc.append(item)

    book.toc = tuple(toc)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = spine
    epub.write_epub(out_path, book)
    return out_path


def assemble(
    store: RunStore,
    source_path: str,
    out_path: str | None = None,
    out_format: str = "epub",
) -> str:
    """生成译文文件（默认 EPUB）。

    out_format="epub"（默认）：
      - 原文是 EPUB → 按原模板回填，保留排版/资源；
      - 原文是纯文本 → 生成一个规范的 EPUB（标题 h1 + 段落 p）。
    out_format="txt"：无论原文格式，按章重建为纯文本。
    """
    m = store.load_manifest()
    title_zh = m.get("title_translated") or ""
    if out_format == "txt":
        return _assemble_text(store, out_path or _default_out(source_path, "txt", title_zh))
    # epub
    out_path = out_path or _default_out(source_path, "epub", title_zh)
    if m["fmt"] == "epub":
        return _assemble_epub(store, source_path, out_path)
    # fb2 / text → 从章节数据生成规范 EPUB
    return _build_epub_from_chapters(store, out_path)
