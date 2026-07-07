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
_HTML_EXTS = (".xhtml", ".html", ".htm")
_VERTICAL_MARKERS = (
    re.compile(rb"(?:-epub-|-webkit-)?writing-mode\s*:\s*(?:vertical-rl|vertical-lr|tb-rl)", re.I),
    re.compile(rb"page-progression-direction\s*=\s*['\"]rtl['\"]", re.I),
    re.compile(rb"\bclass\s*=\s*['\"][^'\"]*\bvrtl\b", re.I),
)
_HORIZONTAL_OVERRIDE_ID = "trans-novel-horizontal-override"
_BILINGUAL_STYLE_ID = "tn-bilingual-style"
_BILINGUAL_CSS = """\
.tn-source {
  font-size: 0.88em;
  line-height: 1.55;
  color: #6b6b6b;
  background-color: #f4f3f0;
  padding: 0.5em 0.8em;
  border-radius: 5px;
  margin: 0.2em 0 1em;
}
@media (prefers-color-scheme: dark) {
  .tn-source {
    color: #a8a8a8;
    background-color: #2a2a2a;
    box-shadow: inset 0 0 0 1px rgba(255,255,255,0.14);
  }
}
"""


def _sanitize_filename(name: str, fallback: str = "translated") -> str:
    name = _ILLEGAL_FN.sub(" ", name or "").strip().strip(".")
    name = re.sub(r"\s+", " ", name)
    return name[:120] or fallback


def _default_out(
    source_path: str,
    out_format: str,
    title: str | None = None,
    *,
    bilingual: bool = False,
) -> str:
    """Return the default export path under the input file's ``output`` folder."""
    ext = ".epub" if out_format == "epub" else ".txt"
    output_dir = os.path.join(os.path.dirname(os.path.abspath(source_path)), "output")
    os.makedirs(output_dir, exist_ok=True)
    if title and title.strip():
        # 保留给显式调用方使用；默认 assemble 不传书名译名。
        return os.path.join(output_dir, _sanitize_filename(title) + ext)
    base, _ = os.path.splitext(source_path)
    suffix = ".zh-bi" if bilingual else ".zh"
    return os.path.join(
        output_dir,
        f"{os.path.basename(base)}{suffix}{ext}",
    )


def bilingual_out_path(out_path: str) -> str:
    """调用方显式指定了 out_path 时，派生双语版路径：stem 追加 -bi。"""
    base, ext = os.path.splitext(out_path)
    return f"{base}-bi{ext}"


def _ch_title(c: dict) -> str:
    """章节展示标题：优先译名，回退原标题。"""
    return (c.get("title_translated") or c.get("title") or "").strip()


def _seg_text(seg) -> str:
    return seg.target if (seg.target and seg.target.strip()) else seg.source


def _epub_lang(lang: str | None) -> str:
    """EPUB 元数据语言码；中文目标默认标成简体中文。"""
    normalized = (lang or "").strip().replace("_", "-").lower()
    if normalized in {"", "zh", "zh-cn", "zh-hans", "cn"}:
        return "zh-Hans"
    return lang or "zh-Hans"


def _merged_paragraphs(chapter: Chapter) -> list[tuple[str, str, str]]:
    """把章内 Segment 合并为段落，cont 续段并回上一段。返回 [(kind, target, source), ...]。"""
    paras: list[list[str]] = []  # 每段累积的译文片段
    srcs: list[list[str]] = []  # 每段累积的原文片段
    kinds: list[str] = []
    for s in chapter.segments:
        if not s.source.strip():
            continue
        if s.cont and paras:
            paras[-1].append(_seg_text(s))
            srcs[-1].append(s.source)
        else:
            paras.append([_seg_text(s)])
            srcs.append([s.source])
            kinds.append(s.kind)
    return [(k, "".join(p), "".join(sr)) for k, p, sr in zip(kinds, paras, srcs)]


def _bilingual_source(source: str, target: str) -> str:
    """双语原文去重：原文为空白，或与译文相同（翻译回退到原文）时不输出原文。"""
    return source if (source.strip() and source != target) else ""


# ── 纯文本 ──────────────────────────────────────────────────────────────────
def _assemble_text(
    store: RunStore,
    out_path: str,
    *,
    bilingual: bool = False,
    order: str = "target_first",
) -> str:
    m = store.load_manifest()
    chapter_blocks: list[str] = []
    for c in m["chapters"]:
        ch = store.load_chapter(c["index"])
        blocks: list[str] = []
        for kind, target, source in _merged_paragraphs(ch):
            src = (
                _bilingual_source(source, target)
                if (bilingual and kind != KIND_HEADING)
                else ""
            )
            if not src:
                blocks.append(target)
            elif order == "source_first":
                blocks.extend((src, target))
            else:
                blocks.extend((target, src))
        chapter_blocks.append("\n\n".join(blocks))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n\n".join(chapter_blocks) + "\n")
    return out_path


# ── EPUB ────────────────────────────────────────────────────────────────────
def _render_chapter_html(
    chapter: Chapter,
    *,
    bilingual: bool = False,
    order: str = "target_first",
) -> str:
    soup = BeautifulSoup(chapter.template or "", "html.parser")
    # 合并 cont 续段：续段文本并回其所属 anchor 元素
    by_anchor: dict[str, str] = {}
    src_by_anchor: dict[str, str] = {}
    kind_by_anchor: dict[str, str] = {}
    cur_anchor: str | None = None
    for s in chapter.segments:
        if s.cont and cur_anchor is not None:
            by_anchor[cur_anchor] += _seg_text(s)
            src_by_anchor[cur_anchor] += s.source
        elif s.anchor:
            cur_anchor = s.anchor
            by_anchor[cur_anchor] = _seg_text(s)
            src_by_anchor[cur_anchor] = s.source
            kind_by_anchor[cur_anchor] = s.kind
    for anchor, text in by_anchor.items():
        el = soup.find(True, attrs={"data-tn-id": anchor})
        if el is None:
            continue
        el.clear()
        el.append(text)
        del el["data-tn-id"]
        if not bilingual or kind_by_anchor.get(anchor) == KIND_HEADING:
            continue
        src = _bilingual_source(src_by_anchor.get(anchor, ""), text)
        if not src:
            continue
        src_el = soup.new_tag("p")
        src_el["class"] = ["tn-source", "ibooks-dark-theme-use-custom-text-color"]
        src_el.append(src)
        if order == "source_first":
            el.insert_before(src_el)
        else:
            el.insert_after(src_el)
    return str(soup)


def _base_no_frag(href: str) -> str:
    """取 href 的文件名（去目录、去 #锚点），用于跨文件相对路径匹配。"""
    return os.path.basename((href or "").split("#", 1)[0])


def _attr_str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _rewrite_opf_metadata(
    data: bytes,
    *,
    book_title: str,
    lang: str,
    force_horizontal: bool,
) -> bytes:
    """更新 OPF 元数据：书名可选改写，译后语言改为目标语言，竖排源书改横排方向。"""
    try:
        soup = BeautifulSoup(data, "xml")
        if book_title:
            title_el = soup.find("dc:title") or soup.find("title")
            if title_el is not None:
                title_el.clear()
                title_el.append(book_title)

        lang_el = soup.find("dc:language") or soup.find("language")
        if lang_el is None:
            metadata = soup.find("metadata")
            if metadata is not None:
                lang_el = soup.new_tag("dc:language")
                metadata.append(lang_el)
        if lang_el is not None:
            lang_el.clear()
            lang_el.append(lang)

        if force_horizontal:
            for spine in soup.find_all("spine"):
                spine["page-progression-direction"] = "ltr"
        return soup.encode()
    except Exception:
        return data
    return data


def _epub_looks_vertical(zf: zipfile.ZipFile) -> bool:
    """粗略检测 EPUB 是否声明了竖排排版。"""
    for info in zf.infolist():
        low = info.filename.lower()
        if not low.endswith((".opf", ".css", ".xhtml", ".html", ".htm")):
            continue
        try:
            data = zf.read(info.filename)
        except Exception:
            continue
        if any(marker.search(data) for marker in _VERTICAL_MARKERS):
            return True
    return False


def _rewrite_html_document(
    data: bytes | str,
    *,
    lang: str,
    force_horizontal: bool,
    bilingual: bool = False,
) -> bytes:
    """给 XHTML/HTML 写入译后语言；必要时注入横排覆盖样式/双语原文样式。"""
    try:
        text = data.decode("utf-8") if isinstance(data, bytes) else data
        soup = BeautifulSoup(text, "html.parser")
        html = soup.find("html")
        if html is None:
            return text.encode("utf-8")
        html["lang"] = lang
        html["xml:lang"] = lang
        classes = html.get("class")
        if isinstance(classes, list) and "vrtl" in classes:
            html["class"] = [c for c in classes if c != "vrtl"]

        if force_horizontal and soup.find(id=_HORIZONTAL_OVERRIDE_ID) is None:
            head = soup.find("head")
            if head is None:
                head = soup.new_tag("head")
                html.insert(0, head)
            style = soup.new_tag("style", id=_HORIZONTAL_OVERRIDE_ID)
            style.string = (
                "html, body { "
                "writing-mode: horizontal-tb !important; "
                "-epub-writing-mode: horizontal-tb !important; "
                "-webkit-writing-mode: horizontal-tb !important; "
                "direction: ltr !important; "
                "text-orientation: mixed !important; "
                "} "
                ".vrtl, .vertical, [class*=\"vrtl\"] { "
                "writing-mode: horizontal-tb !important; "
                "-epub-writing-mode: horizontal-tb !important; "
                "-webkit-writing-mode: horizontal-tb !important; "
                "direction: ltr !important; "
                "}"
            )
            head.append(style)

        if bilingual and soup.find(id=_BILINGUAL_STYLE_ID) is None:
            head = soup.find("head")
            if head is None:
                head = soup.new_tag("head")
                html.insert(0, head)
            style = soup.new_tag("style", id=_BILINGUAL_STYLE_ID)
            style.string = _BILINGUAL_CSS
            head.append(style)
        return str(soup).encode("utf-8")
    except Exception:
        return data if isinstance(data, bytes) else data.encode("utf-8")


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
                t = title_by_base.get(_base_no_frag(_attr_str(content.get("src"))))
                if t:
                    label.clear()
                    label.append(t)
            return soup.encode()
        # EPUB3 nav.xhtml：只改 epub:type="toc" 的导航，避免误改 landmarks / page-list
        soup = BeautifulSoup(data, "html.parser")
        toc_navs = [n for n in soup.find_all("nav")
                    if "toc" in (_attr_str(n.get("epub:type"))
                                 or _attr_str(n.get("type"))).split()]
        scopes = toc_navs or [soup]  # 找不到带类型的 toc nav 时退回全局
        for scope in scopes:
            for a in scope.find_all("a", href=True):
                t = title_by_base.get(_base_no_frag(_attr_str(a.get("href"))))
                if t:
                    a.clear()
                    a.append(t)
        return str(soup).encode("utf-8")
    except Exception:
        return data


def _assemble_epub(
    store: RunStore,
    source_path: str,
    out_path: str,
    *,
    bilingual: bool = False,
    order: str = "target_first",
) -> str:
    m = store.load_manifest()
    target_lang = _epub_lang(m.get("target_lang", "zh"))
    # href -> 渲染后的 XHTML
    rendered: dict[str, str] = {}
    for c in m["chapters"]:
        ch = store.load_chapter(c["index"])
        if ch.href and ch.template:
            rendered[ch.href] = _render_chapter_html(
                ch, bilingual=bilingual, order=order
            )

    # 目录标题映射（文件名 → 译名）；书名保持原文，不改 OPF 主标题。
    title_by_base: dict[str, str] = {}
    for c in m["chapters"]:
        base = _base_no_frag(c.get("href") or "")
        t = _ch_title(c)
        if base and t:
            title_by_base[base] = t
    raw_meta = m.get("meta")
    meta = raw_meta if isinstance(raw_meta, dict) else {}
    raw_toc_entries = meta.get("toc_entries", [])
    toc_entries = raw_toc_entries if isinstance(raw_toc_entries, list) else []
    for entry in toc_entries:
        if not isinstance(entry, dict):
            continue
        href = entry.get("href")
        title_value = entry.get("title_translated") or entry.get("title")
        base = _base_no_frag(href if isinstance(href, str) else "")
        title = title_value.strip() if isinstance(title_value, str) else ""
        if base and title:
            title_by_base[base] = title
    book_title = ""

    with zipfile.ZipFile(source_path, "r") as zin:
        force_horizontal = _epub_looks_vertical(zin)
        infos = zin.infolist()
        with zipfile.ZipFile(out_path, "w") as zout:
            for info in infos:
                name = info.filename
                low = name.lower()
                data = zin.read(name)
                if name in rendered:
                    zout.writestr(
                        info,
                        _rewrite_html_document(
                            rendered[name],
                            lang=target_lang,
                            force_horizontal=force_horizontal,
                            bilingual=bilingual,
                        ),
                    )
                elif name == "mimetype":
                    zout.writestr(info, data, zipfile.ZIP_STORED)
                elif low.endswith(".opf"):
                    zout.writestr(
                        info,
                        _rewrite_opf_metadata(
                            data,
                            book_title=book_title,
                            lang=target_lang,
                            force_horizontal=force_horizontal,
                        ),
                    )
                elif low.endswith(".ncx"):
                    zout.writestr(info, _rewrite_toc(data, title_by_base, is_ncx=True))
                elif low.endswith(_HTML_EXTS):
                    if _is_nav(data):
                        data = _rewrite_toc(data, title_by_base, is_ncx=False)
                    zout.writestr(
                        info,
                        _rewrite_html_document(
                            data,
                            lang=target_lang,
                            force_horizontal=force_horizontal,
                            bilingual=bilingual,
                        ),
                    )
                else:
                    zout.writestr(info, data)
    return out_path


def _is_nav(data: bytes) -> bool:
    return b"epub:type" in data and b"toc" in data


def _inject_bilingual_style(
    out_path: str, chapter_filenames: set[str], lang: str
) -> None:
    """ebooklib 写盘时按模板重建每章 <head>，内联样式会被丢弃；这里对写好的 zip
    做一次后处理，把双语样式补回各章节 head（复用 _rewrite_html_document）。"""
    with zipfile.ZipFile(out_path, "r") as zin:
        infos = zin.infolist()
        entries = {info.filename: zin.read(info.filename) for info in infos}
    tmp_path = out_path + ".tmp"
    with zipfile.ZipFile(tmp_path, "w") as zout:
        for info in infos:
            data = entries[info.filename]
            if os.path.basename(info.filename) in chapter_filenames:
                data = _rewrite_html_document(
                    data,
                    lang=lang,
                    force_horizontal=False,
                    bilingual=True,
                )
            zout.writestr(info, data)
    os.replace(tmp_path, out_path)


def _build_epub_from_chapters(
    store: RunStore,
    out_path: str,
    *,
    bilingual: bool = False,
    order: str = "target_first",
) -> str:
    """从章节数据生成一个规范的 EPUB3（用于纯文本输入），使用 ebooklib。"""
    from html import escape

    from ebooklib import epub

    m = store.load_manifest()
    title = m.get("title", "translated")
    lang = _epub_lang(m.get("target_lang", "zh"))

    book = epub.EpubBook()
    book.set_identifier(f"trans-novel-{title}")
    book.set_title(title)
    book.set_language(lang)

    spine: list = ["nav"]
    toc: list = []
    chapter_filenames: set[str] = set()
    for c in m["chapters"]:
        ch = store.load_chapter(c["index"])
        ch_title = _ch_title(c) or ch.title
        body_parts = []
        for kind, target, source in _merged_paragraphs(ch):
            tag = "h1" if kind == KIND_HEADING else "p"
            target_html = f"<{tag}>{escape(target)}</{tag}>"
            src = (
                _bilingual_source(source, target)
                if (bilingual and kind != KIND_HEADING)
                else ""
            )
            if not src:
                body_parts.append(target_html)
                continue
            src_html = (
                '<p class="tn-source ibooks-dark-theme-use-custom-text-color">'
                f"{escape(src)}</p>"
            )
            if order == "source_first":
                body_parts.extend((src_html, target_html))
            else:
                body_parts.extend((target_html, src_html))
        fname = f"ch{c['index']}.xhtml"
        chapter_filenames.add(fname)
        item = epub.EpubHtml(title=ch_title, file_name=fname, lang=lang)
        item.content = (
            f'<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{lang}">'
            f"<head><title>{escape(ch_title)}</title></head>"
            f"<body>{''.join(body_parts)}</body></html>"
        )
        book.add_item(item)
        spine.append(item)
        toc.append(item)

    book.toc = toc
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = spine
    epub.write_epub(out_path, book)
    if bilingual:
        _inject_bilingual_style(out_path, chapter_filenames, lang)
    return out_path


def assemble(
    store: RunStore,
    source_path: str,
    out_path: str | None = None,
    out_format: str = "epub",
    *,
    bilingual: bool = False,
    order: str = "target_first",
) -> str:
    """生成译文文件（默认 EPUB）。

    out_format="epub"（默认）：
      - 原文是 EPUB → 按原模板回填，保留排版/资源；
      - 原文是纯文本 → 生成一个规范的 EPUB（标题 h1 + 段落 p）。
    out_format="txt"：无论原文格式，按章重建为纯文本。
    bilingual=True 时额外输出原文（淡背景块），order 控制译文/原文先后。
    """
    m = store.load_manifest()
    if out_format == "txt":
        out_path = out_path or _default_out(source_path, "txt", "", bilingual=bilingual)
        return _assemble_text(store, out_path, bilingual=bilingual, order=order)
    # epub
    out_path = out_path or _default_out(source_path, "epub", "", bilingual=bilingual)
    if m["fmt"] == "epub":
        return _assemble_epub(
            store, source_path, out_path, bilingual=bilingual, order=order
        )
    # fb2 / text → 从章节数据生成规范 EPUB
    return _build_epub_from_chapters(store, out_path, bilingual=bilingual, order=order)
