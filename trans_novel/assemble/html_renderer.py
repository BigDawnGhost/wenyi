"""DOM 渲染：把 Chapter/Segment 译文回填到 BeautifulSoup 节点。

本模块负责将 Segment 译文回填到 HTML 模板的 data-tn-id 锚点节点上，
处理 cont 续段合并、双语原文插入、日文 ruby 保留和行内标签恢复。
渲染函数只操作 DOM 并返回 HTML 字符串，不执行文件 I/O。
"""

from __future__ import annotations

from html import escape

from bs4 import BeautifulSoup
from bs4.element import Comment, Tag

from ..ingest.models import KIND_HEADING, Segment
from .writer_common import _bilingual_source, _ordered_pair, _seg_text

# 双语原文样式 ID，用于在 <head> 中注入或检测已有样式
_BILINGUAL_STYLE_ID = "tn-bilingual-style"

# 双语原文淡化和深色模式适配样式
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

# 行内图片等元素的元数据键和属性名
_INLINE_META_KEY = "epub_inline"
_INLINE_ID_ATTR = "data-tn-inline-id"
_LINE_WRAPPER_ATTR = "data-tn-line"


def _render_paragraph_html(
    kind: str,
    target: str,
    source: str,
    *,
    bilingual: bool,
    order: str,
    preserve_source_style: bool = True,
    heading_level: int | None = None,
) -> list[str]:
    """渲染单个段落为 HTML 片段列表。

    - heading_level 不为 None 时用 h{level}（html_writer 用可配级数）；
    - heading_level 为 None 时 heading 统一用 h1（epub_writer 新建 EPUB）。
    - preserve_source_style=True 时原文块用纯 tn-source 类；
    - preserve_source_style=False 时追加 ibooks-dark-theme-use-custom-text-color。
    """
    if kind == KIND_HEADING:
        level = heading_level if heading_level is not None else 1
        target_html = f"<h{level}>{escape(target)}</h{level}>"
    else:
        target_html = f"<p>{escape(target)}</p>"
    src = _bilingual_source(source, target) if (bilingual and kind != KIND_HEADING) else ""
    if not src:
        return [target_html]
    source_class = (
        "tn-source"
        if preserve_source_style
        else "tn-source ibooks-dark-theme-use-custom-text-color"
    )
    src_html = f'<p class="{source_class}">{escape(src)}</p>'
    first, second = _ordered_pair(src_html, target_html, order)
    return [first, second]


def _japanese_ruby_source(element: Tag, source_lang: str) -> str:
    """日语双语原文保留 ruby 注音，并拍平其它文本内联标签。"""
    normalized_lang = source_lang.strip().replace("_", "-").lower()
    if not (normalized_lang == "ja" or normalized_lang.startswith("ja-")):
        return ""
    if element.find("ruby") is None:
        return ""

    fragment = BeautifulSoup(str(element), "html.parser")
    root = fragment.find(element.name)
    if not isinstance(root, Tag):
        return ""
    for comment in list(root.find_all(string=lambda node: isinstance(node, Comment))):
        comment.extract()
    for tag in list(
        root.find_all(
            [
                "audio",
                "canvas",
                "embed",
                "hr",
                "iframe",
                "img",
                "math",
                "object",
                "script",
                "source",
                "style",
                "svg",
                "video",
            ]
        )
    ):
        tag.decompose()
    ruby_tags = {"ruby", "rb", "rt", "rp", "rtc", "br"}
    for tag in list(root.find_all(True)):
        if tag.name not in ruby_tags:
            tag.unwrap()
            continue
        for attr in ("id", "name", "data-tn-id", _INLINE_ID_ATTR, _LINE_WRAPPER_ATTR):
            tag.attrs.pop(attr, None)
    return root.decode_contents()


def _append_source(soup: BeautifulSoup, element: Tag, source: str, markup: str) -> None:
    """向双语原文块写入纯文本，或写入已净化的日语 ruby 片段。"""
    if not markup:
        element.append(source)
        return
    fragment = BeautifulSoup(markup, "html.parser")
    for child in list(fragment.contents):
        element.append(child.extract())


def _append_text_with_breaks(soup: BeautifulSoup, element: Tag, text: str) -> None:
    """向元素追加文本，并把译文换行转换为 XHTML ``br``。"""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for index, line in enumerate(lines):
        if line:
            element.append(line)
        if index + 1 < len(lines):
            element.append(soup.new_tag("br"))


def _replace_block_content(
    soup: BeautifulSoup,
    el: Tag,
    text: str,
    meta: dict[str, object],
) -> None:
    """用译文替换块内容，按元数据恢复图片，并按译文换行生成 ``br``。"""
    raw_inline = meta.get(_INLINE_META_KEY)
    inline = raw_inline if isinstance(raw_inline, dict) else {}
    raw_nodes = inline.get("nodes")
    nodes = raw_nodes if isinstance(raw_nodes, list) else []
    source_length = inline.get("source_length")
    if not isinstance(source_length, int) or source_length < 0:
        source_length = 0

    # 收集行内节点（图片等），按原文偏移比例映射到译文位置
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
            _append_text_with_breaks(soup, el, text[cursor:target_offset])
        el.append(node)
        cursor = target_offset
    if cursor < len(text):
        _append_text_with_breaks(soup, el, text[cursor:])


def _render_segments_html(
    template: str,
    segments: list[Segment],
    *,
    render_meta_by_anchor: dict[str, dict[str, object]] | None = None,
    bilingual: bool = False,
    order: str = "target_first",
    preserve_source_style: bool = False,
    source_lang: str = "",
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
        src = (
            _bilingual_source(src_by_anchor.get(anchor, ""), text)
            if bilingual and kind_by_anchor.get(anchor) != KIND_HEADING
            else ""
        )
        source_markup = _japanese_ruby_source(el, source_lang) if src else ""
        line_wrapper = el.has_attr(_LINE_WRAPPER_ATTR)
        render_meta = (
            render_meta_by_anchor.get(anchor, {})
            if render_meta_by_anchor is not None
            else stored_meta_by_anchor.get(anchor, {})
        )
        _replace_block_content(soup, el, text, render_meta)
        del el["data-tn-id"]
        if not src:
            continue
        # p 的原文可作为相邻段落插入；li/blockquote 则必须留在原容器内，
        # 避免生成 <ul><li>...</li><p>...</p></ul> 之类的非法列表结构，
        # 同时保留引用块的语义和样式。
        nested_source = el.name in {"li", "blockquote"}
        src_el = soup.new_tag("span" if line_wrapper else "div" if nested_source else "p")
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
        _append_source(soup, src_el, src, source_markup)
        if line_wrapper and order == "source_first":
            el.insert_before(src_el)
            src_el.insert_after(soup.new_tag("br"))
        elif line_wrapper:
            el.insert_after(src_el)
            el.insert_after(soup.new_tag("br"))
        elif nested_source and order == "source_first":
            el.insert(0, src_el)
        elif nested_source:
            el.append(src_el)
        elif order == "source_first":
            el.insert_before(src_el)
        else:
            el.insert_after(src_el)
    # br 拆行包装只用于提供独立回填锚点；完成后去掉 span，恢复干净 DOM。
    for wrapper in list(soup.find_all(True, attrs={_LINE_WRAPPER_ATTR: True})):
        wrapper.unwrap()
    return str(soup)


def _render_chapter_html(
    chapter: Chapter,
    *,
    bilingual: bool = False,
    order: str = "target_first",
    preserve_source_style: bool = False,
    source_lang: str = "",
) -> str:
    """回填一个旧式"每章一个模板"的 HTML/EPUB 章节。

    该包装仍供普通 HTML 输出和 0.3.x 以前的 EPUB 状态使用；新 EPUB 状态
    由 :func:`_render_segments_html` 按物理资源聚合回填。
    """
    return _render_segments_html(
        chapter.template or "",
        chapter.segments,
        bilingual=bilingual,
        order=order,
        preserve_source_style=preserve_source_style,
        source_lang=source_lang,
    )
