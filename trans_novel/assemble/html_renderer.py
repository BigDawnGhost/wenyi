"""DOM 渲染：把 Chapter/Segment 译文回填到 BeautifulSoup 节点。

本模块负责将 Segment 译文回填到 HTML 模板的 data-tn-id 锚点节点上，
处理 cont 续段合并、双语原文插入、日文 ruby 保留、注释链接恢复和行内标签。
渲染函数只操作 DOM 并返回 HTML 字符串，不执行文件 I/O。
"""

from __future__ import annotations

import hashlib
from html import escape

from bs4 import BeautifulSoup
from bs4.element import Comment, ProcessingInstruction, Tag

from ..ingest.epub_toc import resolve_epub_href
from ..ingest.models import KIND_HEADING, Chapter, Segment
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
_ANNOTATION_META_KEY = "epub_annotations"
_ANNOTATION_ID_ATTR = "data-tn-annotation-id"
_LINE_WRAPPER_ATTR = "data-tn-line"
_SOURCE_ANCHOR_PREFIX = "tn-source-"


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

    目前由 ``epub_writer._build_epub_from_chapters`` 调用：
    - heading_level 为 None 时 heading 用 h1；传入正整数则用 h{level}。
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


def _bilingual_source_markup(
    element: Tag,
    source_lang: str,
    *,
    resource_href: str,
    source_link_targets: dict[tuple[str, str], str],
) -> str:
    """为双语原文保留注释链接，以及日语原文的 ruby 注音。

    原文注释在源 EPUB 中已经拥有准确位置，无需复用译文定位结果。这里只
    保留注释根及其后代；其它普通内联标签仍拍平成干净文本。克隆节点中的
    ``id``/``name`` 会移除，避免与译文侧保留的原节点产生重复锚点。
    """
    normalized_lang = source_lang.strip().replace("_", "-").lower()
    keep_ruby = normalized_lang == "ja" or normalized_lang.startswith("ja-")
    has_annotation = (
        element.has_attr(_ANNOTATION_ID_ATTR)
        or element.find(True, attrs={_ANNOTATION_ID_ATTR: True}) is not None
    )
    if not has_annotation and (not keep_ruby or element.find("ruby") is None):
        return ""

    fragment = BeautifulSoup(str(element), "html.parser")
    root = fragment.find(element.name)
    if not isinstance(root, Tag):
        return ""

    root_is_annotation = root.has_attr(_ANNOTATION_ID_ATTR)
    retained: set[int] = set()
    for annotation in root.find_all(True, attrs={_ANNOTATION_ID_ATTR: True}):
        retained.add(id(annotation))
        retained.update(id(descendant) for descendant in annotation.find_all(True))
    if root_is_annotation:
        retained.add(id(root))
        retained.update(id(descendant) for descendant in root.find_all(True))
    if keep_ruby:
        for ruby in root.find_all("ruby"):
            retained.add(id(ruby))
            retained.update(id(descendant) for descendant in ruby.find_all(True))

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

    if not keep_ruby:
        for tag in list(root.find_all(["rt", "rp"])):
            tag.decompose()

    for tag in list(root.find_all(True)):
        if id(tag) not in retained:
            tag.unwrap()
            continue
        for attr in (
            "id",
            "name",
            "data-tn-id",
            _INLINE_ID_ATTR,
            _ANNOTATION_ID_ATTR,
            _LINE_WRAPPER_ATTR,
        ):
            tag.attrs.pop(attr, None)
    for attr in (
        "id",
        "name",
        "data-tn-id",
        _INLINE_ID_ATTR,
        _ANNOTATION_ID_ATTR,
        _LINE_WRAPPER_ATTR,
    ):
        root.attrs.pop(attr, None)

    # 译文继续使用原书 fragment；原文镜像只在目标也有原文块时改写到
    # synthetic source anchor。路径和 query 原样保留，故跨 XHTML 链接仍
    # 按原书相对关系解析；未命中映射时保留原链接，避免制造悬空锚点。
    links = [root] if root.name == "a" else []
    links.extend(root.find_all("a", href=True))
    for link in links:
        raw_href = link.get("href")
        if not isinstance(raw_href, str):
            continue
        resolved = resolve_epub_href(resource_href, raw_href)
        source_anchor = source_link_targets.get((resolved.resource_href, resolved.fragment))
        if resolved.external or not resolved.fragment or not source_anchor:
            continue
        path_and_query, separator, _fragment = raw_href.partition("#")
        if separator:
            link["href"] = f"{path_and_query}#{source_anchor}"
    return str(root) if root_is_annotation else root.decode_contents()


def _append_source(soup: BeautifulSoup, element: Tag, source: str, markup: str) -> None:
    """向双语原文块写入纯文本，或写入已净化的注释/ruby 片段。"""
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


def _merge_epub_render_meta(
    stored: dict[str, object],
    fresh: dict[str, object],
) -> dict[str, object]:
    """合并持久化的定位结果与从原 EPUB 重建的临时 DOM 元数据。

    原书重解析得到的 ``items`` 和内联节点位置是 DOM 的权威来源；模型生成的
    目标文本定位只存在章节状态中，不能被新解析出的元数据覆盖。
    """
    merged = dict(stored)
    merged.update(fresh)
    stored_raw = stored.get(_ANNOTATION_META_KEY)
    fresh_raw = fresh.get(_ANNOTATION_META_KEY)
    stored_annotations = stored_raw if isinstance(stored_raw, dict) else {}
    fresh_annotations = fresh_raw if isinstance(fresh_raw, dict) else {}
    if stored_annotations or fresh_annotations:
        annotations = dict(stored_annotations)
        annotations.update(fresh_annotations)
        for key in ("target_digest", "placements"):
            if key in stored_annotations:
                annotations[key] = stored_annotations[key]
        merged[_ANNOTATION_META_KEY] = annotations
    return merged


def _clean_annotation_attrs(node: Tag) -> None:
    """移除仅供回填定位使用的临时属性，避免其泄漏到成品 EPUB。"""
    node.attrs.pop(_ANNOTATION_ID_ATTR, None)
    for descendant in node.find_all(True, attrs={_ANNOTATION_ID_ATTR: True}):
        descendant.attrs.pop(_ANNOTATION_ID_ATTR, None)


def _range_marker_nodes(root: Tag, marker_text: str) -> list[Tag]:
    """从范围链接中取出脚注标记节点，丢弃待替换的源文正文节点。"""
    if not marker_text:
        return []
    candidates = root.find_all(["sup", "sub"])
    for node in reversed(candidates):
        if node.get_text("", strip=True) == marker_text:
            return [node.extract()]
    for node in reversed(root.find_all(True)):
        if node.get_text("", strip=True) == marker_text and not node.find(True):
            return [node.extract()]
    return []


def _fallback_annotation_node(
    root: Tag,
    *,
    mode: str,
    marker_text: str,
) -> Tag:
    """把无法可靠定位的链接降级为段末标记，同时保留原链接属性。"""
    _clean_annotation_attrs(root)
    if mode != "range":
        return root
    markers = _range_marker_nodes(root, marker_text)
    root.clear()
    if markers:
        for marker in markers:
            root.append(marker)
    else:
        root.append(marker_text or "↩")
    return root


def _annotation_restorations(
    el: Tag,
    text: str,
    meta: dict[str, object],
) -> tuple[list[tuple[int, int, Tag]], list[tuple[int, int, int, Tag, list[Tag]]], list[Tag]]:
    """提取注释 DOM，并分成点定位、范围定位和安全降级三组。"""
    raw_annotations = meta.get(_ANNOTATION_META_KEY)
    annotations = raw_annotations if isinstance(raw_annotations, dict) else {}
    raw_items = annotations.get("items")
    items = raw_items if isinstance(raw_items, list) else []
    raw_placements = annotations.get("placements")
    placements = raw_placements if isinstance(raw_placements, list) else []
    placement_by_id = {
        placement["id"]: placement
        for placement in placements
        if isinstance(placement, dict) and isinstance(placement.get("id"), str)
    }
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    digest_matches = annotations.get("target_digest") == digest

    points: list[tuple[int, int, Tag]] = []
    ranges: list[tuple[int, int, int, Tag, list[Tag]]] = []
    fallbacks: list[Tag] = []
    pending_ranges: list[tuple[int, int, int, Tag, list[Tag], str]] = []
    for order, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        annotation_id = item.get("id")
        mode = item.get("mode")
        if not isinstance(annotation_id, str) or mode not in {"point", "range"}:
            continue
        root = el.find(True, attrs={_ANNOTATION_ID_ATTR: annotation_id})
        if not isinstance(root, Tag):
            continue
        root.extract()
        _clean_annotation_attrs(root)
        marker_text = item.get("marker_text")
        marker_text = marker_text if isinstance(marker_text, str) else ""
        placement = placement_by_id.get(annotation_id)
        start = placement.get("target_start") if isinstance(placement, dict) else None
        end = placement.get("target_end") if isinstance(placement, dict) else None
        status = placement.get("status") if isinstance(placement, dict) else None
        method = placement.get("method") if isinstance(placement, dict) else None
        rejected = {"fallback", "failed", "invalid", "missing", "stale"}
        usable = (
            digest_matches
            and isinstance(start, int)
            and not isinstance(start, bool)
            and isinstance(end, int)
            and not isinstance(end, bool)
            and 0 <= start <= end <= len(text)
            and str(status or "").lower() not in rejected
            and str(method or "").lower() not in rejected
        )
        if not usable:
            source_start = item.get("source_start")
            source_end = item.get("source_end")
            source_length = annotations.get("source_length")
            # Only whole-source wraps (e.g. <h1><a>CHAPTER 1</a></h1>) keep the
            # translation inside the link. Partial inline links still fall back to ↩.
            covers_whole_source = (
                mode == "range"
                and not marker_text
                and len(items) == 1
                and isinstance(source_start, int)
                and not isinstance(source_start, bool)
                and isinstance(source_end, int)
                and not isinstance(source_end, bool)
                and isinstance(source_length, int)
                and not isinstance(source_length, bool)
                and source_start == 0
                and source_end == source_length > 0
            )
            if covers_whole_source:
                pending_ranges.append((0, len(text), order, root, [], marker_text))
                continue
            fallbacks.append(_fallback_annotation_node(root, mode=mode, marker_text=marker_text))
            continue
        assert isinstance(start, int) and not isinstance(start, bool)
        assert isinstance(end, int) and not isinstance(end, bool)
        if mode == "point" and start == end:
            points.append((start, order, root))
            continue
        if mode == "range" and start < end:
            markers = _range_marker_nodes(root, marker_text)
            pending_ranges.append((start, end, order, root, markers, marker_text))
            continue
        fallbacks.append(_fallback_annotation_node(root, mode=mode, marker_text=marker_text))

    # HTML 链接不能相互交叉或嵌套。异常范围统一降级，避免生成破损 DOM。
    last_end = -1
    for start, end, order, root, markers, marker_text in sorted(pending_ranges):
        if start < last_end:
            root.clear()
            if markers:
                for marker in markers:
                    root.append(marker)
            else:
                root.append(marker_text or "↩")
            fallbacks.append(root)
            continue
        ranges.append((start, end, order, root, markers))
        last_end = end
    safe_points: list[tuple[int, int, Tag]] = []
    for offset, order, root in points:
        if any(start < offset < end for start, end, _order, _root, _markers in ranges):
            fallbacks.append(root)
        else:
            safe_points.append((offset, order, root))
    return safe_points, ranges, fallbacks


def _render_text_with_nodes(
    soup: BeautifulSoup,
    el: Tag,
    text: str,
    nodes: list[tuple[int, int, Tag]],
    ranges: list[tuple[int, int, int, Tag, list[Tag]]],
    fallbacks: list[Tag],
) -> None:
    """按目标文本偏移回填普通内联节点、注释点和非重叠注释范围。"""
    ordered_nodes = sorted(nodes, key=lambda value: (value[0], value[1]))
    node_index = 0

    def append_until(
        parent: Tag,
        start: int,
        end: int,
        *,
        include_end: bool = True,
    ) -> None:
        nonlocal node_index
        cursor = start
        while node_index < len(ordered_nodes) and (
            ordered_nodes[node_index][0] < end
            or (include_end and ordered_nodes[node_index][0] == end)
        ):
            offset, _order, node = ordered_nodes[node_index]
            node_index += 1
            offset = min(max(offset, cursor), end)
            if offset > cursor:
                _append_text_with_breaks(soup, parent, text[cursor:offset])
            parent.append(node)
            cursor = offset
        if cursor < end:
            _append_text_with_breaks(soup, parent, text[cursor:end])

    # annotate 通常已把 PI 提到块前；此处再兜底，避免漏网 PI 被 clear 掉。
    if el.parent is not None:
        for node in list(el.descendants):
            if isinstance(node, ProcessingInstruction):
                el.insert_before(node.extract())
    el.clear()
    cursor = 0
    for start, end, _order, root, markers in sorted(ranges):
        append_until(el, cursor, start)
        root.clear()
        # 范围结束边界上的点注释是原链接之后的兄弟，不能塞进 a 形成嵌套链接。
        append_until(root, start, end, include_end=False)
        for marker in markers:
            root.append(marker)
        el.append(root)
        cursor = end
    append_until(el, cursor, len(text))
    # 多条降级注释挤在段末时，相邻链接之间原本没有任何分隔文本，脚注数字会
    # 连写成一串（如 11、12、13 会读成 111213）；插入顿号让它们可辨读。
    for index, fallback in enumerate(fallbacks):
        if index > 0:
            el.append("、")
        el.append(fallback)


def _replace_block_content(
    soup: BeautifulSoup,
    el: Tag,
    text: str,
    meta: dict[str, object],
) -> None:
    """用译文替换块内容，并恢复普通内联节点与可跳转注释链接。"""
    # 列表项常直接把 ``a`` 本身作为翻译块。通常只需保留链接外壳；若其中
    # 还带 sup/sub 注释号，则必须先取出标记，避免 clear() 一并删除。
    self_markers: list[Tag] = []
    self_annotation_id = el.get(_ANNOTATION_ID_ATTR)
    if isinstance(self_annotation_id, str):
        raw_annotations = meta.get(_ANNOTATION_META_KEY)
        annotations = raw_annotations if isinstance(raw_annotations, dict) else {}
        raw_items = annotations.get("items")
        items = raw_items if isinstance(raw_items, list) else []
        item = next(
            (
                value
                for value in items
                if isinstance(value, dict) and value.get("id") == self_annotation_id
            ),
            {},
        )
        marker_text = item.get("marker_text")
        self_markers = _range_marker_nodes(
            el,
            marker_text if isinstance(marker_text, str) else "",
        )
        el.attrs.pop(_ANNOTATION_ID_ATTR, None)
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
        restored.append((target_offset, len(nodes) + order, node))

    # 普通内联节点必须先从原 DOM 中取出：范围链接可能同时包裹图片，若先
    # 提取并清空链接根，后续便无法找回其中的原子节点。
    annotation_points, annotation_ranges, annotation_fallbacks = _annotation_restorations(
        el, text, meta
    )
    # 注释节点与普通内联节点共享稳定排序；同偏移下点状注释排在普通节点前。
    restored = list(annotation_points) + restored

    _render_text_with_nodes(
        soup,
        el,
        text,
        restored,
        annotation_ranges,
        annotation_fallbacks,
    )
    for marker in self_markers:
        el.append(marker)


def _segment_render_maps(
    segments: list[Segment],
) -> tuple[
    dict[str, str],
    dict[str, str],
    dict[str, str],
    dict[str, dict[str, object]],
]:
    """按 anchor 合并续段，返回译文、原文、类型和持久化元数据映射。"""
    by_anchor: dict[str, str] = {}
    src_by_anchor: dict[str, str] = {}
    kind_by_anchor: dict[str, str] = {}
    stored_meta_by_anchor: dict[str, dict[str, object]] = {}
    current_anchor: str | None = None
    for segment in segments:
        if segment.cont and current_anchor is not None:
            by_anchor[current_anchor] += _seg_text(segment)
            src_by_anchor[current_anchor] += segment.source
        elif segment.anchor:
            current_anchor = segment.anchor
            by_anchor[current_anchor] = _seg_text(segment)
            src_by_anchor[current_anchor] = segment.source
            kind_by_anchor[current_anchor] = segment.kind
            stored_meta_by_anchor[current_anchor] = segment.meta
    return by_anchor, src_by_anchor, kind_by_anchor, stored_meta_by_anchor


def _index_soup_ids(soup: BeautifulSoup) -> tuple[set[str], dict[str, Tag]]:
    """一次 find_all 同时建立已占用 id/name 集合和 data-tn-id → Tag 索引。

    回填需要对每个锚点做可能数百次查找；若每次都重新 ``soup.find()``，单页耗时与
    「段数 × DOM 规模」成正比。这里只遍历一遍全页标签，后续查找降为 O(1) 字典查找。
    """
    occupied: set[str] = set()
    tn_id_index: dict[str, Tag] = {}
    for node in soup.find_all(True):
        for attr in ("id", "name"):
            value = node.get(attr)
            if isinstance(value, str) and value:
                occupied.add(value)
        tn_id = node.get("data-tn-id")
        if isinstance(tn_id, str) and tn_id:
            tn_id_index[tn_id] = node
    return occupied, tn_id_index


def _build_source_anchor_ids(
    by_anchor: dict[str, str],
    src_by_anchor: dict[str, str],
    kind_by_anchor: dict[str, str],
    tn_id_index: dict[str, Tag],
    occupied: set[str],
) -> dict[str, str]:
    """为实际输出的原文块分配稳定且不与原书冲突的 synthetic ID。"""
    occupied = set(occupied)  # 本函数会不断加入新分配的 id，不能直接复用调用方的集合
    assigned: dict[str, str] = {}
    for anchor, target in by_anchor.items():
        if kind_by_anchor.get(anchor) == KIND_HEADING:
            continue
        source = _bilingual_source(src_by_anchor.get(anchor, ""), target)
        if not source or anchor not in tn_id_index:
            continue
        base = f"{_SOURCE_ANCHOR_PREFIX}{anchor}"
        candidate = base
        suffix = 2
        while candidate in occupied:
            candidate = f"{base}-{suffix}"
            suffix += 1
        assigned[anchor] = candidate
        occupied.add(candidate)
    return assigned


def _render_segments_html(
    template: str,
    segments: list[Segment],
    *,
    render_meta_by_anchor: dict[str, dict[str, object]] | None = None,
    bilingual: bool = False,
    order: str = "target_first",
    preserve_source_style: bool = False,
    source_lang: str = "",
    resource_href: str = "",
    source_ids_by_anchor: dict[str, str] | None = None,
    source_link_targets: dict[tuple[str, str], str] | None = None,
) -> str:
    """把同一物理 HTML 资源内的译文按锚点一次性回填。

    EPUB 的逻辑章节边界可以落在同一个 XHTML 中，也可以跨越多个 XHTML。
    因此真正的回填单位是物理资源而不是 ``Chapter``；调用方须先把属于同一
    ``resource_href`` 的 Segment 聚合后再调用本函数。

    ``preserve_source_style`` 开启时复用原块的 class/style 并不注入
    淡化样式；``tn-source`` 仅作为结构标记保留。
    """
    soup = BeautifulSoup(template, "html.parser")
    by_anchor, src_by_anchor, kind_by_anchor, stored_meta_by_anchor = _segment_render_maps(segments)
    # 一次建索引，后续按 anchor 查找 data-tn-id 节点都是 O(1)，避免每个锚点都
    # 重新 soup.find() 扫一遍全页 DOM。
    occupied_ids, tn_id_index = _index_soup_ids(soup)
    if bilingual and source_ids_by_anchor is None:
        source_ids_by_anchor = _build_source_anchor_ids(
            by_anchor,
            src_by_anchor,
            kind_by_anchor,
            tn_id_index,
            occupied_ids,
        )
    source_ids_by_anchor = source_ids_by_anchor or {}
    if bilingual and source_link_targets is None:
        # 直接调用本函数时仍支持同 XHTML 内链接；完整 EPUB 导出会传入
        # 全书映射，从而同时覆盖跨资源链接。
        from ..ingest.epub_reader import _fragment_anchor_map

        source_link_targets = {
            (resource_href, fragment): source_ids_by_anchor[segment_anchor]
            for fragment, segment_anchor in _fragment_anchor_map(template).items()
            if fragment
            and isinstance(segment_anchor, str)
            and segment_anchor in source_ids_by_anchor
        }
    source_link_targets = source_link_targets or {}
    for anchor, text in by_anchor.items():
        el = tn_id_index.get(anchor)
        if el is None:
            continue
        src = (
            _bilingual_source(src_by_anchor.get(anchor, ""), text)
            if bilingual and kind_by_anchor.get(anchor) != KIND_HEADING
            else ""
        )
        source_markup = (
            _bilingual_source_markup(
                el,
                source_lang,
                resource_href=resource_href,
                source_link_targets=source_link_targets,
            )
            if src
            else ""
        )
        line_wrapper = el.has_attr(_LINE_WRAPPER_ATTR)
        stored_meta = stored_meta_by_anchor.get(anchor, {})
        fresh_meta = (
            render_meta_by_anchor.get(anchor, {}) if render_meta_by_anchor is not None else {}
        )
        render_meta = _merge_epub_render_meta(stored_meta, fresh_meta)
        if text != src_by_anchor.get(anchor, ""):
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
        source_id = source_ids_by_anchor.get(anchor)
        if source_id:
            src_el["id"] = source_id
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
    for node in soup.find_all(True, attrs={_ANNOTATION_ID_ATTR: True}):
        node.attrs.pop(_ANNOTATION_ID_ATTR, None)
    for node in soup.find_all(True, attrs={_INLINE_ID_ATTR: True}):
        node.attrs.pop(_INLINE_ID_ATTR, None)
    return str(soup)


def _render_chapter_html(
    chapter: Chapter,
    *,
    bilingual: bool = False,
    order: str = "target_first",
    preserve_source_style: bool = False,
    source_lang: str = "",
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
        source_lang=source_lang,
        resource_href=chapter.href or "",
    )
