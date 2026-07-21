"""EPUB 读取器（纯标准库 + BeautifulSoup）。

EPUB 即一个 zip：
  META-INF/container.xml → 指向 OPF
  OPF → manifest（资源清单）+ spine（阅读顺序）

读取时先按 spine 提取物理 XHTML 资源，再根据 NCX/NAV 的顶层目录锚点
切成逻辑 Chapter。因此 Chapter 与 XHTML 不再是一对一：切章之后，每个
Segment 的 ``resource_href`` 仍记录它所属的物理资源，writer 据此聚合回填。
"""

from __future__ import annotations

import os
import posixpath
import xml.etree.ElementTree as ET
import zipfile

from bs4 import BeautifulSoup, UnicodeDammit
from bs4.element import Comment, NavigableString, Tag

from .epub_chapters import get_chapter_split_strategy
from .epub_toc import parse_toc_entries, resolve_epub_href
from .models import KIND_HEADING, KIND_TEXT, Chapter, Document, Segment

_CONTAINER = "META-INF/container.xml"
_BLOCK_TAGS = {
    "p",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "li",
    "blockquote",
    "td",
    "th",
    "dt",
    "dd",
}
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_INLINE_META_KEY = "epub_inline"
_INLINE_ID_ATTR = "data-tn-inline-id"
_ATOMIC_INLINE_TAGS = {
    "audio",
    "br",
    "canvas",
    "embed",
    "hr",
    "iframe",
    "img",
    "math",
    "object",
    "svg",
    "video",
}


def _preserved_inline_roots(block: Tag) -> list[Tag]:
    """返回需要原样回填的非文本节点，并尽量保留其无文字包装标签。"""
    roots: list[Tag] = []
    seen: set[int] = set()
    for candidate in block.find_all(True):
        is_atomic = candidate.name in _ATOMIC_INLINE_TAGS
        is_empty_anchor = (
            candidate.name == "a"
            and not candidate.get_text(strip=True)
            and (candidate.has_attr("id") or candidate.has_attr("name"))
        )
        if not is_atomic and not is_empty_anchor:
            continue

        root = candidate
        parent = root.parent
        while (
            isinstance(parent, Tag)
            and parent is not block
            and parent.name not in _BLOCK_TAGS
            and not parent.get_text(strip=True)
        ):
            root = parent
            parent = root.parent
        if id(root) not in seen:
            seen.add(id(root))
            roots.append(root)
    return roots


def _segment_content(block: Tag, anchor: str) -> tuple[str, dict[str, object]]:
    """提取可翻译文本，并给内联非文本节点写入稳定 ID 和位置元数据。"""
    roots = _preserved_inline_roots(block)
    root_ids = {id(node) for node in roots}
    text_parts: list[str] = []
    node_offsets: list[tuple[Tag, int]] = []
    raw_length = 0

    def walk(parent: Tag) -> None:
        """递归收集正文文本节点，并记录需保留节点的源文偏移。"""
        nonlocal raw_length
        for child in parent.children:
            if isinstance(child, Tag):
                if child.name == "rt":
                    # 振假名是注音而非正文；保留在模板中，不送给模型翻译。
                    continue
                if id(child) in root_ids:
                    node_offsets.append((child, raw_length))
                else:
                    walk(child)
            elif isinstance(child, NavigableString) and not isinstance(child, Comment):
                value = str(child)
                text_parts.append(value)
                raw_length += len(value)

    walk(block)
    raw_text = "".join(text_parts)
    text = raw_text.strip()
    if not text:
        return "", {}

    leading = len(raw_text) - len(raw_text.lstrip())
    source_length = len(text)
    nodes: list[dict[str, object]] = []
    for index, (node, raw_offset) in enumerate(node_offsets):
        inline_id = f"{anchor}_inline_{index}"
        offset = min(max(raw_offset - leading, 0), source_length)
        placement = (
            "before"
            if offset == 0
            else "after"
            if offset == source_length
            else "inline"
        )
        node[_INLINE_ID_ATTR] = inline_id
        nodes.append(
            {
                "id": inline_id,
                "tag": node.name,
                "placement": placement,
                "offset": offset,
            }
        )

    meta: dict[str, object] = {}
    if nodes:
        meta[_INLINE_META_KEY] = {
            "version": 1,
            "source_length": source_length,
            "nodes": nodes,
        }
    return text, meta


def _find_opf_path(zf: zipfile.ZipFile) -> str:
    """从 container.xml 解析 EPUB 包文档的 zip 内路径。"""
    data = zf.read(_CONTAINER)
    root = ET.fromstring(data)
    # container.xml 用了默认命名空间，按 localname 匹配
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] == "rootfile":
            path = el.attrib.get("full-path", "").strip()
            if path:
                return path
    raise ValueError("EPUB 损坏：container.xml 未找到有效的 rootfile full-path")


def _zip_href(base_path: str, href: str) -> str:
    """Resolve an EPUB-relative href to a normalized zip member path."""
    return resolve_epub_href(base_path, href).resource_href


def _parse_opf(zf: zipfile.ZipFile, opf_path: str) -> tuple[str, list[str], list[str]]:
    """返回 (书名, spine 顺序的 XHTML zip 路径列表, TOC/NAV 文件路径列表)。"""
    root = ET.fromstring(zf.read(opf_path))

    def local(tag: str) -> str:
        """去掉 XML 命名空间并返回标签本地名。"""
        return tag.rsplit("}", 1)[-1]

    title = ""
    manifest: dict[str, tuple[str, str, str]] = {}  # id -> (href, media-type, properties)
    spine_ids: list[str] = []
    toc_ids: list[str] = []

    for el in root.iter():
        name = local(el.tag)
        if name == "title" and not title and el.text:
            title = el.text.strip()
        elif name == "item":
            item_id = el.attrib.get("id", "").strip()
            if not item_id:
                continue
            manifest[item_id] = (
                el.attrib.get("href", ""),
                el.attrib.get("media-type", ""),
                el.attrib.get("properties", ""),
            )
        elif name == "itemref":
            idref = el.attrib.get("idref", "").strip()
            if idref:
                spine_ids.append(idref)
        elif name == "spine":
            toc = el.attrib.get("toc")
            if toc:
                toc_ids.append(toc)

    hrefs: list[str] = []
    for sid in spine_ids:
        if sid not in manifest:
            continue
        href, media, _props = manifest[sid]
        if "html" not in media and not href.endswith((".xhtml", ".html", ".htm")):
            continue
        resolved_href = _zip_href(opf_path, href)
        if resolved_href and resolved_href not in hrefs:
            # 同一物理资源可被 spine 重复引用，但 zip 中仍只有一份
            # XHTML；只标注一次，避免生成无法回填的第二套锚点。
            hrefs.append(resolved_href)

    # EPUB3 NAV 是主目录；没有 NAV 时优先使用 spine.toc 指定的
    # EPUB2 NCX。其它目录仍保留供标题回填，但不与主目录混合切章。
    nav_ids = [
        item_id
        for item_id, (_href, _media, props) in manifest.items()
        if "nav" in props.split()
    ]
    ncx_ids = [
        item_id
        for item_id, (_href, media, _props) in manifest.items()
        if media == "application/x-dtbncx+xml"
    ]
    ordered_toc_ids = nav_ids + toc_ids + ncx_ids
    toc_paths: list[str] = []
    for item_id in ordered_toc_ids:
        if item_id not in manifest:
            continue
        href = _zip_href(opf_path, manifest[item_id][0])
        if href and href not in toc_paths:
            toc_paths.append(href)
    return title, hrefs, toc_paths


def _decode_markup(data: bytes) -> str:
    """按 XML/HTML 声明与字节特征解码 XHTML，最后才使用 UTF-8 替换兜底。"""
    decoded = UnicodeDammit(data).unicode_markup
    return decoded if decoded is not None else data.decode("utf-8", errors="replace")


def _looks_like_internal_title(title: str, href: str, book_title: str = "") -> bool:
    """判断 XHTML title 是否只是内部文件名或重复的全书书名。"""
    base = posixpath.basename(href).rsplit(".", 1)[0]
    stripped = title.strip()
    return (bool(base) and stripped == base) or (bool(book_title) and stripped == book_title.strip())


def annotate_epub_resource(
    html: str,
    resource_index: int,
    href: str,
    *,
    book_title: str = "",
    skip_navigation: bool = False,
) -> tuple[str, list[Segment], str]:
    """标注单个物理 XHTML，返回标题、Segment 和可回填模板。

    锚点使用物理资源序号而非最终 Chapter 序号，因此即使改用其它
    逻辑切章策略，writer 重建模板时仍能生成相同的 ``data-tn-id``。
    """
    soup = BeautifulSoup(html, "html.parser")
    segments: list[Segment] = []
    idx = 0
    for el in soup.find_all(_BLOCK_TAGS):
        if skip_navigation and _inside_navigation_list(el):
            # NAV 可以同时是 spine 中的可见目录页。目录 li 中嵌套着
            # a/ol，若当普通段落回填会清空整棵目录结构；nav 内独立的
            # “Contents”等 heading/p 仍可安全作为普通正文翻译。
            continue
        # 跳过嵌套在另一个块级元素内的块（避免重复计数，如 blockquote 里的 p）
        if any(getattr(p, "name", None) in _BLOCK_TAGS for p in el.parents):
            continue
        # 带文字的内联 id/name 包装会在回填纯译文时被拍平。先把它
        # 改成同位置的空锚点，便可复用现有内联非文本节点恢复机制。
        for descendant in list(el.find_all(True)):
            if not descendant.get_text(strip=True):
                continue
            anchor_attrs = {
                key: descendant.attrs.pop(key)
                for key in ("id", "name")
                if key in descendant.attrs
            }
            if anchor_attrs:
                marker = soup.new_tag("a")
                marker.attrs.update(anchor_attrs)
                descendant.insert_before(marker)

        anchor = f"tn{resource_index}_{idx}"
        text, meta = _segment_content(el, anchor)
        if not text:
            continue
        el["data-tn-id"] = anchor
        kind = KIND_HEADING if el.name in _HEADING_TAGS else KIND_TEXT
        segments.append(
            Segment(
                index=idx,
                source=text,
                kind=kind,
                anchor=anchor,
                resource_href=href,
                meta=meta,
            )
        )
        idx += 1

    # 物理资源的备用标题：首个 heading → 非内部文件名/书名的
    # <title> → 无标题。逻辑章标题在后续切章时直接取完整 TOC 节点。
    # 一些 EPUB 把 XHTML 文件名写进 <title>，如 cUH.xhtml 的 <title>cUH</title>，
    # 或把全书书名写进每个 <title>，这不是读者可见章节标题，不能进入目录或标题翻译。
    title = ""
    for s in segments:
        if s.kind == KIND_HEADING:
            title = s.source
            break
    if not title and soup.title and soup.title.string:
        candidate = soup.title.string.strip()
        if not _looks_like_internal_title(candidate, href, book_title):
            title = candidate

    return title, segments, str(soup)


def _inside_navigation_list(element: Tag) -> bool:
    """判断块元素是否属于 EPUB3 ``nav`` 的目录列表结构。

    这里只保护 ``li`` 及其内部块，避免普通回填清空链接和嵌套 ``ol``；
    位于 ``nav`` 内但不属于列表的可见标题/说明文字仍应进入翻译流程。
    """
    inside_nav = False
    inside_list_item = element.name == "li"
    for parent in element.parents:
        if not isinstance(parent, Tag):
            continue
        if parent.name == "li":
            inside_list_item = True
        elif parent.name == "nav":
            inside_nav = True
            break
    return inside_nav and inside_list_item


def _fragment_anchor_map(template: str) -> dict[str, str | None]:
    """把 XHTML 中的 id/name 定位到 Segment 锚点。

    值为 ``None`` 表示 ID 确实存在，但它位于该资源最后一个
    可翻译块之后；这与“fragment 根本不存在”必须区分。
    """
    soup = BeautifulSoup(template, "html.parser")
    mapping: dict[str, str | None] = {}
    for node in soup.find_all(True):
        identifiers = [node.get("id"), node.get("name")]
        if not any(isinstance(value, str) and value for value in identifiers):
            continue
        block = node if node.has_attr("data-tn-id") else node.find_parent(attrs={"data-tn-id": True})
        if not isinstance(block, Tag):
            block = node.find_next(attrs={"data-tn-id": True})
        raw_anchor = block.get("data-tn-id") if isinstance(block, Tag) else None
        anchor = raw_anchor if isinstance(raw_anchor, str) and raw_anchor else None
        for value in identifiers:
            if isinstance(value, str) and value:
                mapping.setdefault(value, anchor)
    return mapping


def _logical_chapters(
    resources: list[dict[str, object]],
    toc_entries: list[dict[str, object]],
) -> tuple[list[Chapter], str, str]:
    """按当前策略把物理资源流切成逻辑 Chapter。

    无可用目录边界时回退为每个非空 spine XHTML 一章，与历来行为
    一致。如首个目录边界前仍有正文，它会成为独立前置章，不丢内容。
    """
    all_segments: list[Segment] = []
    anchor_positions: dict[str, int] = {}
    resource_starts: dict[str, int] = {}
    resource_by_href: dict[str, dict[str, object]] = {}
    for resource in resources:
        href = str(resource["href"])
        resource_by_href[href] = resource
        resource_starts[href] = len(all_segments)
        raw_segments = resource.get("segments")
        segments = raw_segments if isinstance(raw_segments, list) else []
        for segment in segments:
            if not isinstance(segment, Segment):
                continue
            if segment.anchor:
                anchor_positions[segment.anchor] = len(all_segments)
            all_segments.append(segment)
    for raw_entry in toc_entries:
        entry = raw_entry
        href = entry.get("resource_href")
        if not isinstance(href, str) or href not in resource_starts:
            continue
        fragment = entry.get("fragment")
        has_fragment = isinstance(fragment, str) and bool(fragment)
        resource = resource_by_href[href]
        raw_fragment_map = resource.get("fragment_anchors")
        fragment_map = raw_fragment_map if isinstance(raw_fragment_map, dict) else {}
        if has_fragment and fragment not in fragment_map:
            # 损坏的 fragment 不能悄悄退回到资源开头，否则会在
            # 错误位置切章，并把首个 heading 的译文写给错误目录项。
            continue
        segment_anchor = fragment_map.get(fragment) if has_fragment else None
        if not has_fragment:
            raw_segments = resource.get("segments")
            resource_segments = raw_segments if isinstance(raw_segments, list) else []
            first = next((segment for segment in resource_segments if isinstance(segment, Segment)), None)
            segment_anchor = first.anchor if first is not None else None
        if isinstance(segment_anchor, str) and segment_anchor in anchor_positions:
            entry["segment_anchor"] = segment_anchor
            entry["boundary_position"] = anchor_positions[segment_anchor]
        elif has_fragment:
            raw_segments = resource.get("segments")
            segment_count = (
                sum(isinstance(segment, Segment) for segment in raw_segments)
                if isinstance(raw_segments, list)
                else 0
            )
            # fragment 存在但位于最后一个文本块之后。
            entry["boundary_position"] = resource_starts[href] + segment_count
        else:
            # 无文字标题页也是有效目录边界：它会在流中占据当前
            # 位置，后续 spine 正文因此仍能归入该逻辑章。
            entry["boundary_position"] = resource_starts[href]

    # NAV <span> 或宽容 NCX 可以用无 href/content 的节点表示“部”。
    # 这类分组节点继承第一个可定位后代的边界，但不继承
    # segment_anchor，以免把子章 heading 的译文误当成分组标题译文。
    toc_paths = {
        str(entry.get("toc_path"))
        for entry in toc_entries
        if isinstance(entry.get("toc_path"), str) and entry.get("toc_path")
    }
    for toc_path in toc_paths:
        path_entries = [
            entry for entry in toc_entries if entry.get("toc_path") == toc_path
        ]
        children: dict[int, list[dict[str, object]]] = {}
        for entry in path_entries:
            parent_index = entry.get("parent_index")
            if isinstance(parent_index, int):
                children.setdefault(parent_index, []).append(entry)
        for entry in reversed(path_entries):
            if isinstance(entry.get("boundary_position"), int):
                continue
            if entry.get("raw_href"):
                # 只有无链接的结构分组可以继承子节点；已显式给出
                # 但无法解析的链接属于损坏数据，不应被悄悄改成别的目标。
                continue
            node_index = entry.get("node_index")
            if not isinstance(node_index, int):
                continue
            descendant = next(
                (
                    child
                    for child in children.get(node_index, [])
                    if isinstance(child.get("boundary_position"), int)
                ),
                None,
            )
            if descendant is not None:
                entry["boundary_position"] = descendant["boundary_position"]
                entry["inherited_boundary_from"] = descendant.get("entry_id")

    strategy = get_chapter_split_strategy()
    ordered_toc_paths = list(
        dict.fromkeys(
            str(entry.get("toc_path"))
            for entry in toc_entries
            if isinstance(entry.get("toc_path"), str) and entry.get("toc_path")
        )
    )
    canonical_toc_path = ""
    boundaries: list[dict[str, object]] = []
    for toc_path in ordered_toc_paths:
        candidates = strategy.select(
            [entry for entry in toc_entries if entry.get("toc_path") == toc_path]
        )
        if candidates:
            # EPUB3 NAV 仍由 _parse_opf 排在 NCX 前；仅当较优先目录
            # 完全无法提供章边界时，才退到下一份可用目录。
            canonical_toc_path = toc_path
            boundaries = candidates
            break

    def boundary_position(entry: dict[str, object]) -> int:
        """返回已由切章策略验证过的整数边界位置。"""
        value = entry.get("boundary_position")
        if not isinstance(value, int):
            raise ValueError("EPUB chapter boundary is missing an integer position")
        return value

    boundaries.sort(key=boundary_position)

    if not boundaries:
        chapters: list[Chapter] = []
        for resource in resources:
            raw_segments = resource.get("segments")
            segments = [s for s in raw_segments if isinstance(s, Segment)] if isinstance(raw_segments, list) else []
            if not segments:
                continue
            for index, segment in enumerate(segments):
                segment.index = index
            chapters.append(
                Chapter(
                    index=len(chapters),
                    title=str(resource.get("title") or ""),
                    segments=segments,
                    href=str(resource.get("href") or "") or None,
                    template=None,
                    meta={"epub_split_strategy": "spine-fallback"},
                )
            )
        return chapters, "spine-fallback", canonical_toc_path

    slices: list[tuple[int, int, dict[str, object] | None]] = []
    first_position = boundary_position(boundaries[0])
    if first_position > 0:
        slices.append((0, first_position, None))
    for index, boundary in enumerate(boundaries):
        start = boundary_position(boundary)
        end = (
            boundary_position(boundaries[index + 1])
            if index + 1 < len(boundaries)
            else len(all_segments)
        )
        if end > start:
            slices.append((start, end, boundary))

    chapters = []
    for start, end, boundary in slices:
        segments = all_segments[start:end]
        for index, segment in enumerate(segments):
            segment.index = index
        if boundary is not None:
            title = str(boundary.get("title") or "")
            toc_entry_id = boundary.get("entry_id")
            first_href = segments[0].resource_href or str(
                boundary.get("resource_href") or ""
            )
        else:
            first_href = segments[0].resource_href or ""
            title = segments[0].source if segments[0].kind == KIND_HEADING else ""
            toc_entry_id = None
        meta: dict[str, object] = {"epub_split_strategy": strategy.name}
        if isinstance(toc_entry_id, str):
            meta["toc_entry_id"] = toc_entry_id
        chapters.append(
            Chapter(
                index=len(chapters),
                title=title,
                segments=segments,
                href=first_href or None,
                template=None,
                meta=meta,
            )
        )
    return chapters, strategy.name, canonical_toc_path


def read_epub(path: str, source_lang: str, target_lang: str) -> Document:
    """按 spine 读取物理资源，再按顶层目录锚点生成逻辑章节。"""
    with zipfile.ZipFile(path, "r") as zf:
        names = set(zf.namelist())
        opf_path = _find_opf_path(zf)
        book_title, hrefs, toc_paths = _parse_opf(zf, opf_path)
        toc_entries = parse_toc_entries(zf, toc_paths)

        resources: list[dict[str, object]] = []
        for resource_index, href in enumerate(hrefs):
            if href not in names:
                continue
            html = _decode_markup(zf.read(href))
            title, segments, template = annotate_epub_resource(
                html,
                resource_index,
                href,
                book_title=book_title,
                skip_navigation=href in toc_paths,
            )
            resources.append(
                {
                    "index": resource_index,
                    "href": href,
                    "title": title,
                    "segments": segments,
                    "template": template,
                    "fragment_anchors": _fragment_anchor_map(template),
                }
            )
        chapters, split_strategy, split_toc_path = _logical_chapters(
            resources, toc_entries
        )
        # XHTML 模板和内联布局都可从原始 EPUB 确定性重建，不写入运行状态。
        # Segment.meta 中其它格式或后续阶段添加的信息仍原样保留。
        for chapter in chapters:
            chapter.template = None
            for segment in chapter.segments:
                segment.meta.pop(_INLINE_META_KEY, None)

    return Document(
        title=book_title or os.path.splitext(os.path.basename(path))[0],
        source_lang=source_lang,
        target_lang=target_lang,
        fmt="epub",
        source_path=os.path.abspath(path),
        chapters=chapters,
        meta={
            "epub_schema": 3,
            "opf_path": opf_path,
            "toc_paths": toc_paths,
            "toc_entries": toc_entries,
            "epub_resources": [
                {"index": resource["index"], "href": resource["href"]}
                for resource in resources
            ],
            "epub_split_strategy": split_strategy,
            "epub_split_toc_path": split_toc_path,
        },
    )
