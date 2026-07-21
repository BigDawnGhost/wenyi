"""回填：把译文写回原格式。

- 纯文本：按章重建，标题 + 段落（空行分隔）。
- EPUB：重开原始 zip，逐条目原样拷贝；按物理 XHTML 聚合逻辑章节中的
  Segment，重建稳定 data-tn-id 模板后一次性回填，非正文资源不动；NCX/NAV
  标题按目录文件和节点序号精确替换。
缺失译文的段回退使用原文，保证不丢内容。
"""

from __future__ import annotations

import os
import re
import zipfile
from html import escape

from bs4 import BeautifulSoup, UnicodeDammit
from bs4.element import Tag

from ..ingest.epub_toc import nav_root_list, nav_toc_scopes
from ..ingest.fb2_reader import read_fb2_binaries
from ..ingest.models import KIND_HEADING, Chapter, Segment
from ..pipeline.runstore import RunStore
from .about import append_about_page

_ILLEGAL_FN = re.compile(r'[\\/:*?"<>|\r\n\t]+')
_HTML_EXTS = (".xhtml", ".html", ".htm")
_VERTICAL_MARKERS = (
    re.compile(
        rb"(?:-epub-|-webkit-)?writing-mode\s*:\s*(?:vertical-rl|vertical-lr|tb-rl)",
        re.I,
    ),
    re.compile(rb"page-progression-direction\s*=\s*['\"]rtl['\"]", re.I),
    re.compile(rb"\bclass\s*=\s*['\"][^'\"]*\bvrtl\b", re.I),
)
_HORIZONTAL_OVERRIDE_ID = "trans-novel-horizontal-override"
_BILINGUAL_STYLE_ID = "tn-bilingual-style"
_IMAGE_EXTENSION_BY_TYPE = {
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/svg+xml": ".svg",
    "image/webp": ".webp",
}
_INLINE_META_KEY = "epub_inline"
_INLINE_ID_ATTR = "data-tn-inline-id"
_XML_ENCODING = re.compile(
    r"(<\?xml[^>]*\bencoding\s*=\s*)(['\"])[^'\"]+\2",
    re.IGNORECASE,
)
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
    """移除跨平台非法文件名字符，并限制名称长度。"""
    name = _ILLEGAL_FN.sub(" ", name or "").strip().strip(".")
    name = re.sub(r"\s+", " ", name)
    return name[:120] or fallback


_OUT_EXT = {"epub": ".epub", "txt": ".txt", "html": ".html", "markdown": ".md"}


def _ensure_parent_dir(path: str) -> None:
    """Create the output directory while allowing a bare filename."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)


def _default_out(
    source_path: str,
    out_format: str,
    title: str | None = None,
    *,
    bilingual: bool = False,
) -> str:
    """Return the default export path under the input file's ``output`` folder."""
    ext = _OUT_EXT.get(out_format, ".epub")
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
    """返回有效译文；译文为空时回退到源文以避免丢内容。"""
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


def _replace_block_content(el: Tag, text: str, meta: dict[str, object]) -> None:
    """用纯译文替换块内容，并按解析元数据恢复图片等非文本节点。"""
    raw_inline = meta.get(_INLINE_META_KEY)
    inline = raw_inline if isinstance(raw_inline, dict) else {}
    raw_nodes = inline.get("nodes")
    nodes = raw_nodes if isinstance(raw_nodes, list) else []
    source_length = inline.get("source_length")
    if not isinstance(source_length, int) or source_length < 0:
        source_length = 0

    restored: list[tuple[int, int, Tag]] = []
    for order, record in enumerate(nodes):
        if not isinstance(record, dict):
            continue
        inline_id = record.get("id")
        offset = record.get("offset")
        if not isinstance(inline_id, str) or not isinstance(offset, int):
            continue
        node = el.find(True, attrs={_INLINE_ID_ATTR: inline_id})
        if not isinstance(node, Tag):
            continue
        node.extract()
        node.attrs.pop(_INLINE_ID_ATTR, None)
        if offset <= 0:
            target_offset = 0
        elif source_length <= 0 or offset >= source_length:
            target_offset = len(text)
        else:
            target_offset = round(offset * len(text) / source_length)
        restored.append((target_offset, order, node))

    el.clear()
    cursor = 0
    for target_offset, _order, node in sorted(restored):
        target_offset = min(max(target_offset, cursor), len(text))
        if target_offset > cursor:
            el.append(text[cursor:target_offset])
        el.append(node)
        cursor = target_offset
    if cursor < len(text):
        el.append(text[cursor:])


# ── 纯文本 ──────────────────────────────────────────────────────────────────
def _assemble_text(
    store: RunStore,
    out_path: str,
    *,
    bilingual: bool = False,
    order: str = "target_first",
) -> str:
    """按章节和段落重建 UTF-8 文本，可选插入双语对照原文。"""
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


# ── markdown ──────────────────────────────────────────────────────────────────
def _assemble_markdown(
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
        level = ch.meta.get("heading_level", 1)
        level = level if isinstance(level, int) and 1 <= level <= 6 else 1
        heading_prefix = "#" * level + " "
        blocks: list[str] = []
        for kind, target, source in _merged_paragraphs(ch):
            if kind == KIND_HEADING:
                target = heading_prefix + target
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
def _render_segments_html(
    template: str,
    segments: list[Segment],
    *,
    render_meta_by_anchor: dict[str, dict[str, object]] | None = None,
    bilingual: bool = False,
    order: str = "target_first",
    preserve_source_style: bool = False,
) -> str:
    """把同一物理 HTML 资源内的译文按锚点一次性回填。

    EPUB 的逻辑章节边界可以落在同一个 XHTML 中，也可以跨越多个 XHTML。
    因此真正的回填单位是物理资源而不是 ``Chapter``；调用方须先把属于同一
    ``resource_href`` 的 Segment 聚合后再调用本函数。

    ``preserve_source_style`` 开启时复用原块的 class/style 并不注入
    淡化样式；``tn-source`` 仅作为结构标记保留。
    """
    soup = BeautifulSoup(template, "html.parser")
    # 合并 cont 续段：续段文本并回其所属 anchor 元素
    by_anchor: dict[str, str] = {}
    src_by_anchor: dict[str, str] = {}
    kind_by_anchor: dict[str, str] = {}
    stored_meta_by_anchor: dict[str, dict[str, object]] = {}
    cur_anchor: str | None = None
    for s in segments:
        if s.cont and cur_anchor is not None:
            by_anchor[cur_anchor] += _seg_text(s)
            src_by_anchor[cur_anchor] += s.source
        elif s.anchor:
            cur_anchor = s.anchor
            by_anchor[cur_anchor] = _seg_text(s)
            src_by_anchor[cur_anchor] = s.source
            kind_by_anchor[cur_anchor] = s.kind
            stored_meta_by_anchor[cur_anchor] = s.meta
    for anchor, text in by_anchor.items():
        el = soup.find(True, attrs={"data-tn-id": anchor})
        if el is None:
            continue
        render_meta = (
            render_meta_by_anchor.get(anchor, {})
            if render_meta_by_anchor is not None
            else stored_meta_by_anchor.get(anchor, {})
        )
        _replace_block_content(el, text, render_meta)
        del el["data-tn-id"]
        if not bilingual or kind_by_anchor.get(anchor) == KIND_HEADING:
            continue
        src = _bilingual_source(src_by_anchor.get(anchor, ""), text)
        if not src:
            continue
        # p 的原文可作为相邻段落插入；li/blockquote 则必须留在原容器内，
        # 避免生成 <ul><li>...</li><p>...</p></ul> 之类的非法列表结构，
        # 同时保留引用块的语义和样式。
        nested_source = el.name in {"li", "blockquote"}
        src_el = soup.new_tag("div" if nested_source else "p")
        source_classes = ["tn-source"]
        if preserve_source_style:
            original_classes = el.get("class")
            if isinstance(original_classes, list):
                source_classes = [str(value) for value in original_classes]
                if "tn-source" not in source_classes:
                    source_classes.append("tn-source")
            original_style = el.get("style")
            if isinstance(original_style, str):
                src_el["style"] = original_style
        else:
            source_classes.append("ibooks-dark-theme-use-custom-text-color")
        src_el["class"] = " ".join(source_classes)
        src_el.append(src)
        if nested_source and order == "source_first":
            el.insert(0, src_el)
        elif nested_source:
            el.append(src_el)
        elif order == "source_first":
            el.insert_before(src_el)
        else:
            el.insert_after(src_el)
    return str(soup)


def _render_chapter_html(
    chapter: Chapter,
    *,
    bilingual: bool = False,
    order: str = "target_first",
    preserve_source_style: bool = False,
) -> str:
    """回填一个旧式“每章一个模板”的 HTML/EPUB 章节。

    该包装仍供普通 HTML 输出和 0.3.x 以前的 EPUB 状态使用；新 EPUB 状态
    由 :func:`_render_segments_html` 按物理资源聚合回填。
    """
    return _render_segments_html(
        chapter.template or "",
        chapter.segments,
        bilingual=bilingual,
        order=order,
        preserve_source_style=preserve_source_style,
    )


def _base_no_frag(href: str) -> str:
    """取 href 的文件名（去目录、去 #锚点），用于跨文件相对路径匹配。"""
    return os.path.basename((href or "").split("#", 1)[0])


def _attr_str(value: object) -> str:
    """把 BeautifulSoup 属性安全收窄为字符串。"""
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
        if isinstance(data, bytes):
            text = UnicodeDammit(data).unicode_markup
            if text is None:
                text = data.decode("utf-8", errors="replace")
        else:
            text = data
        soup = BeautifulSoup(text, "html.parser")
        html = soup.find("html")
        if html is None:
            return text.encode("utf-8")
        html["lang"] = lang
        html["xml:lang"] = lang
        classes = html.get("class")
        if isinstance(classes, list) and "vrtl" in classes:
            html["class"] = " ".join(str(c) for c in classes if c != "vrtl")

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
                '.vrtl, .vertical, [class*="vrtl"] { '
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
        output = _XML_ENCODING.sub(r'\1"utf-8"', str(soup))
        return output.encode("utf-8")
    except Exception:
        return data if isinstance(data, bytes) else data.encode("utf-8")


def _direct_child(parent: Tag | BeautifulSoup, name: str) -> Tag | None:
    """返回 ``parent`` 的首个指定直接子元素。"""
    child = parent.find(name, recursive=False)
    return child if isinstance(child, Tag) else None


def _nav_label_nodes(soup: BeautifulSoup) -> list[tuple[Tag, str]]:
    """按 reader 的 preorder 规则列出 EPUB3 TOC 条目标签及原始 href。

    每个 TOC ``li`` 优先取直接子 ``a``，其次取直接子 ``span``；没有这两种
    标签的 ``li`` 不计入 ``node_index``。分组 ``span`` 也属于可翻译目录项，
    但没有内容目标。嵌套列表递归处理，以保证编号与解析阶段完全一致。
    """
    labels: list[tuple[Tag, str]] = []

    def walk_list(ordered_list: Tag) -> None:
        for child in ordered_list.children:
            if not isinstance(child, Tag) or child.name != "li":
                continue
            label = _direct_child(child, "a") or _direct_child(child, "span")
            if label is not None:
                labels.append((label, _attr_str(label.get("href"))))
            nested = _direct_child(child, "ol")
            if nested is not None:
                walk_list(nested)

    for scope in nav_toc_scopes(soup):
        root = nav_root_list(scope)
        if root is not None:
            walk_list(root)
    return labels


def _ncx_nav_points(soup: BeautifulSoup) -> list[Tag]:
    """按 reader 的直接子节点 preorder 规则列出 NCX ``navPoint``。"""
    nav_map = soup.find("navMap")
    if not isinstance(nav_map, Tag):
        return []
    points: list[Tag] = []

    def walk(parent: Tag) -> None:
        for child in parent.children:
            if not isinstance(child, Tag) or child.name != "navPoint":
                continue
            points.append(child)
            walk(child)

    walk(nav_map)
    return points


def _translated_toc_title(entry: dict[str, object]) -> str:
    """返回一个目录条目的有效译名，缺失时回退原标题。"""
    value = entry.get("title_translated") or entry.get("title")
    return value.strip() if isinstance(value, str) else ""


def _indexed_toc_entries(
    entries: list[dict[str, object]], toc_path: str
) -> dict[int, dict[str, object]]:
    """按 ``toc_path + node_index`` 建立目录节点的精确索引。"""
    indexed: dict[int, dict[str, object]] = {}
    for entry in entries:
        if entry.get("toc_path") != toc_path:
            continue
        node_index = entry.get("node_index")
        if isinstance(node_index, int) and node_index >= 0:
            indexed[node_index] = entry
    return indexed


def _rewrite_toc(
    data: bytes,
    entries_or_legacy_titles: list[dict[str, object]] | dict[str, str],
    *,
    is_ncx: bool,
    toc_path: str = "",
) -> bytes:
    """回填 NCX/NAV 的可见标题，同时原样保留 ``src``/``href``。

    新状态按 ``toc_path + node_index`` 定位，因此同一 XHTML 的多个片段标题
    可以拥有不同译名，也不会因不同目录下的同名文件互相覆盖。为继续导出旧
    状态，传入 ``{basename: title}`` 时仍使用原来的文件名匹配逻辑。
    """
    try:
        exact_entries = (
            _indexed_toc_entries(entries_or_legacy_titles, toc_path)
            if isinstance(entries_or_legacy_titles, list)
            else {}
        )
        legacy_titles = (
            entries_or_legacy_titles
            if isinstance(entries_or_legacy_titles, dict)
            else {}
        )
        if is_ncx:
            soup = BeautifulSoup(data, "xml")
            for node_index, nav_point in enumerate(_ncx_nav_points(soup)):
                nav_label = _direct_child(nav_point, "navLabel")
                label = nav_label.find("text") if nav_label is not None else None
                if not isinstance(label, Tag):
                    continue
                entry = exact_entries.get(node_index)
                if entry is not None:
                    content = _direct_child(nav_point, "content")
                    raw_src = _attr_str(content.get("src")) if content else ""
                    expected = entry.get("raw_href")
                    if isinstance(expected, str) and expected != raw_src:
                        # 源 EPUB 与状态记录不一致时宁可保留原标题，也不改错节点。
                        continue
                    title = _translated_toc_title(entry)
                else:
                    content = _direct_child(nav_point, "content")
                    title = legacy_titles.get(
                        _base_no_frag(_attr_str(content.get("src")) if content else "")
                    )
                if title:
                    label.clear()
                    label.append(title)
            return soup.encode()

        # EPUB3 nav.xhtml：仅枚举 epub:type="toc" 范围内的直接 li 标签。
        soup = BeautifulSoup(data, "html.parser")
        if legacy_titles:
            # 旧状态保留原有的宽松匹配，兼容没有标准 ol/li 结构的 NAV。
            toc_navs = [
                node
                for node in soup.find_all("nav")
                if "toc"
                in (
                    _attr_str(node.get("epub:type"))
                    or _attr_str(node.get("type"))
                ).split()
            ]
            scopes: list[Tag | BeautifulSoup] = toc_navs or [soup]
            for scope in scopes:
                for label in scope.find_all("a", href=True):
                    title = legacy_titles.get(
                        _base_no_frag(_attr_str(label.get("href")))
                    )
                    if title:
                        label.clear()
                        label.append(title)
            return str(soup).encode("utf-8")
        for node_index, (label, raw_href) in enumerate(_nav_label_nodes(soup)):
            entry = exact_entries.get(node_index)
            if entry is not None:
                expected = entry.get("raw_href")
                if isinstance(expected, str) and expected != raw_href:
                    continue
                title = _translated_toc_title(entry)
            else:
                title = legacy_titles.get(_base_no_frag(raw_href))
            if title:
                label.clear()
                label.append(title)
        return str(soup).encode("utf-8")
    except Exception:
        return data


def _epub_resource_specs(meta: dict[str, object]) -> list[tuple[int, str]]:
    """读取新状态中的物理 XHTML 清单，过滤损坏或重复的记录。"""
    raw_resources = meta.get("epub_resources")
    if not isinstance(raw_resources, list):
        return []
    resources: list[tuple[int, str]] = []
    seen: set[str] = set()
    for fallback_index, raw_resource in enumerate(raw_resources):
        if not isinstance(raw_resource, dict):
            continue
        href = raw_resource.get("href")
        if not isinstance(href, str) or not href or href in seen:
            continue
        raw_index = raw_resource.get("index")
        resource_index = raw_index if isinstance(raw_index, int) else fallback_index
        resources.append((resource_index, href))
        seen.add(href)
    return resources


def _segments_by_resource(chapters: list[Chapter]) -> dict[str, list[Segment]]:
    """按源文顺序聚合逻辑章节中的 EPUB Segment 到物理资源。"""
    grouped: dict[str, list[Segment]] = {}
    for chapter in chapters:
        for segment in chapter.segments:
            href = segment.resource_href
            if href:
                grouped.setdefault(href, []).append(segment)
    return grouped


def _render_epub_resources(
    zin: zipfile.ZipFile,
    chapters: list[Chapter],
    meta: dict[str, object],
    *,
    book_title: str,
    bilingual: bool,
    order: str,
    preserve_source_style: bool,
) -> dict[str, str]:
    """从原 EPUB 重建稳定模板，并将每个物理 XHTML 仅渲染一次。

    解析状态只保存 Segment 和 ``resource_href``，原始 EPUB 仍是排版与内联
    元素的权威来源。重新执行确定性的锚点标注，比把整份 XHTML 模板复制到
    每个逻辑章节更节省状态空间，也避免同一物理文件被多章分别写回而覆盖。
    """
    resources = _epub_resource_specs(meta)
    grouped = _segments_by_resource(chapters)
    if not resources or not grouped:
        return {}
    declared_hrefs = {href for _index, href in resources}
    undeclared = sorted(set(grouped) - declared_hrefs)
    if undeclared:
        raise ValueError(
            "EPUB 翻译状态引用了未登记的正文资源：" + ", ".join(undeclared[:3])
        )

    # 延迟导入避免 reader -> models / writer 模块加载期间形成不必要的依赖环。
    from ..ingest.epub_reader import annotate_epub_resource

    names = set(zin.namelist())
    raw_toc_paths = meta.get("toc_paths")
    toc_paths = (
        {path for path in raw_toc_paths if isinstance(path, str)}
        if isinstance(raw_toc_paths, list)
        else set()
    )
    rendered: dict[str, str] = {}
    for resource_index, href in resources:
        segments = grouped.get(href)
        if not segments:
            continue
        if href not in names:
            raise ValueError(f"EPUB 正文资源不存在：{href}")
        source_data = zin.read(href)
        html = UnicodeDammit(source_data).unicode_markup
        if html is None:
            html = source_data.decode("utf-8", errors="replace")
        _title, annotated_segments, template = annotate_epub_resource(
            html,
            resource_index,
            href,
            book_title=book_title,
            skip_navigation=href in toc_paths,
        )

        # 状态和源书不匹配时不能静默漏回填；这种情况通常表示用户替换了原书。
        available_anchors = {
            segment.anchor for segment in annotated_segments if segment.anchor
        }
        required_anchors = {
            segment.anchor
            for segment in segments
            if segment.anchor and not segment.cont
        }
        missing = sorted(required_anchors - available_anchors)
        if missing:
            preview = ", ".join(missing[:3])
            raise ValueError(
                f"EPUB 正文与翻译状态不匹配：{href} 缺少回填锚点 {preview}"
            )

        fresh_by_anchor = {
            segment.anchor: segment
            for segment in annotated_segments
            if segment.anchor
        }
        stored_sources: dict[str, str] = {}
        current_anchor: str | None = None
        for segment in segments:
            if segment.cont and current_anchor is not None:
                stored_sources[current_anchor] += segment.source
            elif segment.anchor:
                current_anchor = segment.anchor
                stored_sources[current_anchor] = segment.source
            else:
                current_anchor = None
        changed_anchors = [
            anchor
            for anchor, source in stored_sources.items()
            if fresh_by_anchor[anchor].source != source
        ]
        if changed_anchors:
            preview = ", ".join(changed_anchors[:3])
            raise ValueError(
                f"EPUB 原文与翻译状态不匹配：{href} 内容已变化（{preview}）"
            )
        fresh_meta_by_anchor = {
            anchor: segment.meta
            for anchor, segment in fresh_by_anchor.items()
        }
        rendered[href] = _render_segments_html(
            template,
            segments,
            render_meta_by_anchor=fresh_meta_by_anchor,
            bilingual=bilingual,
            order=order,
            preserve_source_style=preserve_source_style,
        )
    return rendered


# ── HTML ────────────────────────────────────────────────────────────────────
def _assemble_html(
    store: RunStore,
    source_path: str,
    out_path: str,
    *,
    bilingual: bool = False,
    order: str = "target_first",
    preserve_source_style: bool = False,
) -> str:
    """回填 HTML 原文：逐章渲染 template，拼接为完整 HTML 输出。"""
    m = store.load_manifest()
    raw_meta = m.get("meta")
    meta = raw_meta if isinstance(raw_meta, dict) else {}
    raw_head_html = meta.get("head_html", "")
    head_html = raw_head_html if isinstance(raw_head_html, str) else ""
    # 始终确保 charset 声明，否则浏览器无法正确识别编码导致中文乱码
    if 'charset' not in head_html.replace(' ', '').lower():
        head_html = '<meta charset="utf-8"/>\n' + head_html
    if bilingual and not preserve_source_style and _BILINGUAL_STYLE_ID not in head_html:
        head_html += f'<style id="{_BILINGUAL_STYLE_ID}">\n{_BILINGUAL_CSS}</style>'

    body_parts: list[str] = []
    rendered_epub = False
    if m.get("fmt") == "epub" and _epub_resource_specs(meta):
        chapters = [store.load_chapter(c["index"]) for c in m["chapters"]]
        with zipfile.ZipFile(source_path, "r") as archive:
            rendered = _render_epub_resources(
                archive,
                chapters,
                meta,
                book_title=m.get("title", "") if isinstance(m.get("title"), str) else "",
                bilingual=bilingual,
                order=order,
                preserve_source_style=preserve_source_style,
            )
        for _resource_index, href in _epub_resource_specs(meta):
            resource_html = rendered.get(href)
            if not resource_html:
                continue
            resource_soup = BeautifulSoup(resource_html, "html.parser")
            resource_body = resource_soup.find("body")
            body_parts.append(
                resource_body.decode_contents()
                if isinstance(resource_body, Tag)
                else str(resource_soup)
            )
        rendered_epub = bool(body_parts)

    for c in ([] if rendered_epub else m["chapters"]):
        ch = store.load_chapter(c["index"])
        if ch.template:
            # 复用 EPUB 的章节渲染（替换 data-tn-id → 译文，处理 cont 续段与双语）
            body_parts.append(
                _render_chapter_html(
                    ch,
                    bilingual=bilingual,
                    order=order,
                    preserve_source_style=preserve_source_style,
                )
            )
            continue

        # TXT / Markdown 等无 HTML 模板的输入也必须能导出正文。
        for kind, target, source in _merged_paragraphs(ch):
            if kind == KIND_HEADING:
                level = ch.meta.get("heading_level", 1)
                level = level if isinstance(level, int) and 1 <= level <= 6 else 1
                target_html = f"<h{level}>{escape(target)}</h{level}>"
            else:
                target_html = f"<p>{escape(target)}</p>"
            src = (
                _bilingual_source(source, target)
                if (bilingual and kind != KIND_HEADING)
                else ""
            )
            if not src:
                body_parts.append(target_html)
                continue
            source_html = f'<p class="tn-source">{escape(src)}</p>'
            if order == "source_first":
                body_parts.extend((source_html, target_html))
            else:
                body_parts.extend((target_html, source_html))

    full_html = f"""<!DOCTYPE html>
<html lang="{escape(_epub_lang(m.get("target_lang", "zh")))}">
<head>
{head_html}
</head>
<body>
{"".join(body_parts)}
</body>
</html>"""

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(full_html)
    return out_path


def _assemble_epub(
    store: RunStore,
    source_path: str,
    out_path: str,
    *,
    bilingual: bool = False,
    order: str = "target_first",
    preserve_source_style: bool = False,
) -> str:
    """复制原 EPUB，并按物理资源替换正文、精确回填目录及目标语言元数据。"""
    m = store.load_manifest()
    target_lang = _epub_lang(m.get("target_lang", "zh"))
    raw_meta = m.get("meta")
    meta = raw_meta if isinstance(raw_meta, dict) else {}
    raw_toc_entries = meta.get("toc_entries", [])
    toc_entries: list[dict[str, object]] = (
        [entry for entry in raw_toc_entries if isinstance(entry, dict)]
        if isinstance(raw_toc_entries, list)
        else []
    )
    raw_toc_paths = meta.get("toc_paths")
    toc_paths: set[str] = set()
    if isinstance(raw_toc_paths, list):
        toc_paths.update(
            path for path in raw_toc_paths if isinstance(path, str) and path
        )
    for entry in toc_entries:
        toc_path = entry.get("toc_path")
        if isinstance(toc_path, str) and toc_path:
            toc_paths.add(toc_path)
    ncx_paths = {
        str(entry["toc_path"])
        for entry in toc_entries
        if entry.get("kind") == "ncx" and isinstance(entry.get("toc_path"), str)
    }

    chapters = [store.load_chapter(c["index"]) for c in m["chapters"]]

    # 旧状态没有 node_index/resource_href，继续按 basename 回填；这条链路不能
    # 区分同一 XHTML 的多个 fragment，只作为已有翻译状态的导出兼容层。
    legacy_titles: dict[str, str] = {}
    for chapter_meta in m["chapters"]:
        base = _base_no_frag(chapter_meta.get("href") or "")
        title = _ch_title(chapter_meta)
        if base and title:
            legacy_titles[base] = title
    for entry in toc_entries:
        if not isinstance(entry, dict):
            continue
        href = entry.get("resource_href") or entry.get("href")
        base = _base_no_frag(href if isinstance(href, str) else "")
        title = _translated_toc_title(entry)
        if base and title:
            legacy_titles[base] = title
    book_title = ""

    with zipfile.ZipFile(source_path, "r") as zin:
        force_horizontal = _epub_looks_vertical(zin)
        rendered = _render_epub_resources(
            zin,
            chapters,
            meta,
            book_title=m.get("title", "") if isinstance(m.get("title"), str) else "",
            bilingual=bilingual,
            order=order,
            preserve_source_style=preserve_source_style,
        )
        if not rendered:
            # 旧状态：每个 Chapter 自带一份物理 XHTML 模板。
            for chapter in chapters:
                if chapter.href and chapter.template:
                    rendered[chapter.href] = _render_chapter_html(
                        chapter,
                        bilingual=bilingual,
                        order=order,
                        preserve_source_style=preserve_source_style,
                    )

        infos = zin.infolist()
        with zipfile.ZipFile(out_path, "w") as zout:
            for info in infos:
                name = info.filename
                low = name.lower()
                data = zin.read(name)
                if name == "mimetype":
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
                elif low.endswith(".ncx") or name in ncx_paths:
                    exact = _indexed_toc_entries(toc_entries, name)
                    toc_source: list[dict[str, object]] | dict[str, str] = (
                        toc_entries if exact else legacy_titles
                    )
                    zout.writestr(
                        info,
                        _rewrite_toc(
                            data,
                            toc_source,
                            is_ncx=True,
                            toc_path=name,
                        ),
                    )
                elif low.endswith(_HTML_EXTS):
                    html_data = (
                        rendered[name].encode("utf-8")
                        if name in rendered
                        else data
                    )
                    if name in toc_paths or _is_nav(html_data):
                        exact = _indexed_toc_entries(toc_entries, name)
                        toc_source = toc_entries if exact else legacy_titles
                        html_data = _rewrite_toc(
                            html_data,
                            toc_source,
                            is_ncx=False,
                            toc_path=name,
                        )
                    zout.writestr(
                        info,
                        _rewrite_html_document(
                            html_data,
                            lang=target_lang,
                            force_horizontal=force_horizontal,
                            bilingual=bilingual and not preserve_source_style,
                        ),
                    )
                else:
                    zout.writestr(info, data)
    return out_path


def _is_nav(data: bytes) -> bool:
    """粗略判断 HTML 资源是否包含 EPUB3 目录导航。"""
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
    try:
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
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _build_epub_from_chapters(
    store: RunStore,
    source_path: str,
    out_path: str,
    *,
    bilingual: bool = False,
    order: str = "target_first",
    preserve_source_style: bool = False,
) -> str:
    """从章节数据生成规范 EPUB3，供无原始 EPUB 模板的输入格式使用。"""
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
    image_hrefs: dict[str, str] = {}
    raw_meta = m.get("meta")
    manifest_meta = raw_meta if isinstance(raw_meta, dict) else {}
    if m.get("fmt") == "fb2":
        binaries = read_fb2_binaries(source_path)
        cover_id = manifest_meta.get("fb2_cover_image")
        used_hrefs: set[str] = set()
        for index, (resource_id, (content_type, payload)) in enumerate(
            binaries.items()
        ):
            stem, extension = os.path.splitext(os.path.basename(resource_id))
            safe_stem = _sanitize_filename(stem, f"image-{index}")
            extension = extension.lower() or _IMAGE_EXTENSION_BY_TYPE.get(
                content_type, ".bin"
            )
            href = f"images/{safe_stem}{extension}"
            suffix = 2
            while href in used_hrefs:
                href = f"images/{safe_stem}-{suffix}{extension}"
                suffix += 1
            used_hrefs.add(href)
            image_hrefs[resource_id] = href
            if resource_id == cover_id:
                book.set_cover(href, payload, create_page=True)
            else:
                book.add_item(
                    epub.EpubItem(
                        uid=f"fb2-image-{index}",
                        file_name=href,
                        media_type=content_type,
                        content=payload,
                    )
                )

    for c in m["chapters"]:
        ch = store.load_chapter(c["index"])
        ch_title = _ch_title(c) or ch.title
        body_parts = []
        images_by_position: dict[int, list[str]] = {}
        raw_images = ch.meta.get("fb2_images")
        if isinstance(raw_images, list):
            for image in raw_images:
                if not isinstance(image, dict):
                    continue
                position = image.get("position")
                resource_id = image.get("id")
                if not isinstance(position, int) or not isinstance(resource_id, str):
                    continue
                href = image_hrefs.get(resource_id)
                if href:
                    images_by_position.setdefault(position, []).append(href)

        paragraphs = _merged_paragraphs(ch)
        for position, (kind, target, source) in enumerate(paragraphs):
            body_parts.extend(
                f'<div class="fb2-image"><img src="{escape(href, quote=True)}" '
                'alt=""/></div>'
                for href in images_by_position.get(position, [])
            )
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
            source_class = (
                "tn-source"
                if preserve_source_style
                else "tn-source ibooks-dark-theme-use-custom-text-color"
            )
            src_html = f'<p class="{source_class}">{escape(src)}</p>'
            if order == "source_first":
                body_parts.extend((src_html, target_html))
            else:
                body_parts.extend((target_html, src_html))
        body_parts.extend(
            f'<div class="fb2-image"><img src="{escape(href, quote=True)}" '
            'alt=""/></div>'
            for href in images_by_position.get(len(paragraphs), [])
        )
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
    if bilingual and not preserve_source_style:
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
    preserve_source_style: bool = False,
    about_page: bool = True,
) -> str:
    """生成译文文件（默认 EPUB）。

    out_format="epub"（默认）：
      - 原文是 EPUB → 按原模板回填，保留排版/资源；
      - 原文是纯文本 → 生成一个规范的 EPUB（标题 h1 + 段落 p）。
    out_format="txt"：无论原文格式，按章重建为纯文本。
    out_format="html"：优先回填 HTML 模板，无模板时按章重建。
    out_format="markdown"：无论原文格式，按章重建为 Markdown。
    bilingual=True 时额外输出原文，order 控制译文/原文先后。
    preserve_source_style=True 时原文继承原书正文样式，不注入淡化 CSS。
    about_page=True 时在书末附加“关于此翻译”说明页。
    """
    if out_format not in _OUT_EXT:
        supported = " / ".join(_OUT_EXT)
        raise ValueError(f"不支持的输出格式：{out_format}（支持 {supported}）")

    m = store.load_manifest()
    if out_format == "txt":
        out_path = out_path or _default_out(source_path, "txt", "", bilingual=bilingual)
        _ensure_parent_dir(out_path)
        return _assemble_text(store, out_path, bilingual=bilingual, order=order)
    if out_format == "html":
        out_path = out_path or _default_out(
            source_path, "html", "", bilingual=bilingual
        )
        _ensure_parent_dir(out_path)
        return _assemble_html(
            store,
            source_path,
            out_path,
            bilingual=bilingual,
            order=order,
            preserve_source_style=preserve_source_style,
        )
    if out_format == "markdown":
        out_path = out_path or _default_out(
            source_path, "markdown", "", bilingual=bilingual
        )
        _ensure_parent_dir(out_path)
        return _assemble_markdown(store, out_path, bilingual=bilingual, order=order)
    out_path = out_path or _default_out(source_path, "epub", "", bilingual=bilingual)
    _ensure_parent_dir(out_path)
    if m["fmt"] == "epub":
        result = _assemble_epub(
            store,
            source_path,
            out_path,
            bilingual=bilingual,
            order=order,
            preserve_source_style=preserve_source_style,
        )
    else:
        # fb2 / text / html → 从章节数据生成规范 EPUB
        result = _build_epub_from_chapters(
            store,
            source_path,
            out_path,
            bilingual=bilingual,
            order=order,
            preserve_source_style=preserve_source_style,
        )
    if about_page:
        append_about_page(result, _epub_lang(m.get("target_lang", "zh")))
    return result
