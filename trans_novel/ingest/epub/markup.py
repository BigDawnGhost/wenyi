"""EPUB XHTML parsing, text-slot extraction, and resource annotations."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Collection

from lxml import etree

from trans_novel.epub.markup import is_backlink, is_noteref
from trans_novel.epub.navigation import nav_toc_roots_lxml
from trans_novel.epub.slots import EpubSegmentState, EpubTextSlot, slot_contract_digest
from trans_novel.ingest.epub.package import looks_like_internal_title
from trans_novel.ingest.models import KIND_HEADING, KIND_TEXT, Segment

_BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "td", "th", "dt", "dd"}
_BLOCK_CANDIDATE_TAGS = _BLOCK_TAGS | {"div"}
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_IMMUTABLE_TEXT_TAGS = {"script", "style", "rt", "rp"}
_ATOMIC_TEXT_TAGS = {
    "audio",
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
_WS_RE = re.compile(r"[ \t\r\n\f\v]+")


def element_children(element: etree._Element) -> list[etree._Element]:
    return [child for child in element if isinstance(child.tag, str)]


def element_path(root: etree._Element, element: etree._Element) -> tuple[int, ...]:
    if element is root:
        return ()
    path: list[int] = []
    current = element
    while current is not root:
        parent = current.getparent()
        if parent is None:
            raise ValueError("EPUB element is detached from its resource root")
        children = element_children(parent)
        try:
            path.append(children.index(current))
        except ValueError as error:
            raise ValueError("EPUB element locator is not an element-index path") from error
        current = parent
    return tuple(reversed(path))


def attr_local(element: etree._Element, name: str) -> str:
    for key, value in element.attrib.items():
        if key.rsplit("}", 1)[-1].split(":", 1)[-1] == name:
            return value
    return ""


def _semantic_hints(element: etree._Element) -> list[str]:
    """保留正文块及其祖先显式声明的 XHTML 语义。"""
    hints: list[str] = []
    for node in (element, *element.iterancestors()):
        tag = node.tag.rsplit("}", 1)[-1].lower() if isinstance(node.tag, str) else ""
        for name in ("type", "role"):
            value = attr_local(node, name).strip()
            if value:
                hints.append(f"xhtml:{tag}:{name}={value}")
    return list(dict.fromkeys(hints))


def visible_text(
    element: etree._Element,
    *,
    protected_paths: Collection[tuple[int, ...]] = (),
    root: etree._Element | None = None,
) -> str:
    parts: list[str] = []
    resource_root = root if root is not None else element

    def walk(node: etree._Element) -> None:
        tag = node.tag.rsplit("}", 1)[-1].lower() if isinstance(node.tag, str) else ""
        if (
            tag in _IMMUTABLE_TEXT_TAGS
            or tag in _ATOMIC_TEXT_TAGS
            or is_footnote_marker(node, protected_paths=protected_paths, root=resource_root)
        ):
            return
        if node.text:
            parts.append(node.text)
        for child in node:
            if isinstance(child.tag, str):
                walk(child)
            if child.tail:
                parts.append(child.tail)

    walk(element)
    return _WS_RE.sub(" ", "".join(parts)).strip()


def is_footnote_marker(
    element: etree._Element,
    *,
    protected_paths: Collection[tuple[int, ...]] = (),
    root: etree._Element | None = None,
) -> bool:
    tag = element.tag.rsplit("}", 1)[-1].lower() if isinstance(element.tag, str) else ""
    if tag != "a":
        return False
    epub_type = element.get("{http://www.idpf.org/2007/ops}type", element.get("epub:type", ""))
    if is_noteref(epub_type, element.get("role", "")) or is_backlink(
        epub_type, element.get("role", "")
    ):
        return True
    return root is not None and element_path(root, element) in protected_paths


def block_slots(
    root: etree._Element,
    block: etree._Element,
    *,
    resource_href: str,
    resource_sha256: str,
    parse_mode: str,
    anchor: str,
    protected_paths: Collection[tuple[int, ...]] = (),
) -> list[EpubTextSlot]:
    slots: list[EpubTextSlot] = []
    slot_index = 0

    def add(owner: etree._Element, field: str, value: str | None) -> None:
        nonlocal slot_index
        if value is None:
            return
        slot_index += 1
        slots.append(
            EpubTextSlot(
                id=f"{anchor}:s{slot_index}",
                element_path=element_path(block, owner),
                field=field,
                source_value=value,
            )
        )

    def walk(owner: etree._Element) -> None:
        tag = owner.tag.rsplit("}", 1)[-1].lower() if isinstance(owner.tag, str) else ""
        if (
            tag in _IMMUTABLE_TEXT_TAGS
            or tag in _ATOMIC_TEXT_TAGS
            or is_footnote_marker(owner, protected_paths=protected_paths, root=root)
        ):
            return
        add(owner, "text", owner.text)
        for child in owner:
            if not isinstance(child.tag, str):
                continue
            walk(child)
            add(child, "tail", child.tail)

    walk(block)
    return slots


def designated_slot_values(
    block: etree._Element,
    *,
    protected_paths: Collection[tuple[int, ...]] = (),
    root: etree._Element | None = None,
) -> list[tuple[tuple[int, ...], str, str]]:
    designated: list[tuple[tuple[int, ...], str, str]] = []
    resource_root = root if root is not None else block

    def walk(owner: etree._Element) -> None:
        tag = owner.tag.rsplit("}", 1)[-1].lower() if isinstance(owner.tag, str) else ""
        if (
            tag in _IMMUTABLE_TEXT_TAGS
            or tag in _ATOMIC_TEXT_TAGS
            or is_footnote_marker(owner, protected_paths=protected_paths, root=resource_root)
        ):
            return
        if owner.text is not None:
            designated.append((element_path(block, owner), "text", owner.text))
        for child in owner:
            if not isinstance(child.tag, str):
                continue
            walk(child)
            if child.tail is not None:
                designated.append((element_path(block, child), "tail", child.tail))

    walk(block)
    return designated


def resource_fingerprint(block: etree._Element) -> str:
    return hashlib.sha256(etree.tostring(block, encoding="utf-8", with_tail=False)).hexdigest()


def _is_block_candidate(
    root: etree._Element,
    element: etree._Element,
    note_target_paths: Collection[tuple[int, ...]],
) -> bool:
    local = element.tag.rsplit("}", 1)[-1].lower() if isinstance(element.tag, str) else ""
    return local in _BLOCK_CANDIDATE_TAGS or (
        local == "aside" and element_path(root, element) in note_target_paths
    )


def _note_aside_direct_text(
    root: etree._Element,
    aside: etree._Element,
    *,
    protected_paths: Collection[tuple[int, ...]],
) -> str:
    parts: list[str] = []

    def walk(node: etree._Element) -> None:
        local = node.tag.rsplit("}", 1)[-1].lower() if isinstance(node.tag, str) else ""
        if (
            (node is not aside and local in _BLOCK_CANDIDATE_TAGS)
            or local in _IMMUTABLE_TEXT_TAGS
            or local in _ATOMIC_TEXT_TAGS
            or is_footnote_marker(node, protected_paths=protected_paths, root=root)
        ):
            return
        if node.text:
            parts.append(node.text)
        for child in node:
            if not isinstance(child.tag, str):
                continue
            walk(child)
            if child.tail:
                parts.append(child.tail)

    walk(aside)
    return _WS_RE.sub(" ", "".join(parts)).strip()


def lxml_targets(
    root: etree._Element,
    *,
    skip_navigation: bool,
    protected_paths: Collection[tuple[int, ...]] = (),
    note_target_paths: Collection[tuple[int, ...]] = (),
) -> list[etree._Element]:
    candidates: list[etree._Element] = []
    nav_roots = nav_toc_roots_lxml(root) if skip_navigation else []
    for element in root.iter():
        if not isinstance(element.tag, str) or not _is_block_candidate(
            root, element, note_target_paths
        ):
            continue
        local = element.tag.rsplit("}", 1)[-1].lower()
        if any(
            isinstance(parent.tag, str)
            and parent.tag.rsplit("}", 1)[-1].lower() in _IMMUTABLE_TEXT_TAGS | _ATOMIC_TEXT_TAGS
            for parent in element.iterancestors()
        ):
            continue
        if skip_navigation and any(
            element is nav_root or any(element is node for node in nav_root.iterdescendants())
            for nav_root in nav_roots
        ):
            continue
        descendants = [
            child
            for child in element.iterdescendants()
            if isinstance(child.tag, str)
            and _is_block_candidate(root, child, note_target_paths)
            and visible_text(child, protected_paths=protected_paths, root=root)
        ]
        if local == "li":
            direct_link = next(
                (
                    child
                    for child in element
                    if isinstance(child.tag, str)
                    and child.tag.rsplit("}", 1)[-1].lower() == "a"
                    and visible_text(child, protected_paths=protected_paths, root=root)
                ),
                None,
            )
            if direct_link is not None and not any(
                isinstance(child.tag, str)
                and child.tag.rsplit("}", 1)[-1].lower() in _BLOCK_CANDIDATE_TAGS
                and visible_text(child, protected_paths=protected_paths, root=root)
                for child in direct_link.iterdescendants()
            ):
                candidates.append(direct_link)
                continue
        if descendants:
            continue
        candidates.append(element)
    return [
        candidate
        for candidate in candidates
        if visible_text(candidate, protected_paths=protected_paths, root=root)
    ]


def _runs_for_slots(block: etree._Element, slots: list[EpubTextSlot]) -> list[list[EpubTextSlot]]:
    runs: list[list[EpubTextSlot]] = [[]]
    direct_children = element_children(block)
    for slot in slots:
        if not slot.element_path:
            run_index = 0
        else:
            child_index = slot.element_path[0]
            run_index = sum(
                1
                for child in direct_children[: child_index + 1]
                if child.tag.rsplit("}", 1)[-1].lower() == "br"
            )
        while len(runs) <= run_index:
            runs.append([])
        runs[run_index].append(slot)
    return runs


def _segments_for_blocks(
    root: etree._Element,
    blocks: list[etree._Element],
    *,
    resource_index: int,
    href: str,
    digest: str,
    parse_mode: str,
    protected_paths: Collection[tuple[int, ...]] = (),
) -> list[Segment]:
    segments: list[Segment] = []
    for index, block in enumerate(blocks):
        anchor = f"tn{resource_index}_{index}"
        slots = block_slots(
            root,
            block,
            resource_href=href,
            resource_sha256=digest,
            parse_mode=parse_mode,
            anchor=anchor,
            protected_paths=protected_paths,
        )
        if [
            (tuple(slot.element_path), slot.field, slot.source_value) for slot in slots
        ] != designated_slot_values(block, protected_paths=protected_paths, root=root):
            raise ValueError(f"EPUB source slot coverage mismatch: {href}")
        if not slots:
            continue
        kind = KIND_HEADING if block.tag.rsplit("}", 1)[-1].lower() in _HEADING_TAGS else KIND_TEXT
        for run_index, run_slots in enumerate(_runs_for_slots(block, slots)):
            if not run_slots or not any(slot.source_value.strip() for slot in run_slots):
                continue
            run_anchor = anchor if run_index == 0 else f"{anchor}_br{run_index}"
            if run_anchor != anchor:
                run_slots = [
                    slot.model_copy(update={"id": f"{run_anchor}:s{slot_index}"})
                    for slot_index, slot in enumerate(run_slots, 1)
                ]
            source = _WS_RE.sub(" ", "".join(slot.source_value for slot in run_slots)).strip()
            state = EpubSegmentState(
                resource_href=href,
                resource_sha256=digest,
                block_path=element_path(root, block),
                block_fingerprint=resource_fingerprint(block),
                parse_mode=parse_mode,
                slots=run_slots,
                slot_contract_sha256=slot_contract_digest(run_slots),
            )
            segments.append(
                Segment(
                    index=len(segments),
                    source=source,
                    kind=kind,
                    anchor=run_anchor,
                    resource_href=href,
                    epub_state=state,
                    meta={"semantic_hints": _semantic_hints(block)},
                )
            )
    return segments


def _fragment_anchors(root: etree._Element, segments: list[Segment]) -> dict[str, str | None]:
    def resolve(path: tuple[int, ...]) -> etree._Element:
        current = root
        for child_index in path:
            current = element_children(current)[child_index]
        return current

    ordered_nodes = list(root.iter())
    node_positions = {id(node): index for index, node in enumerate(ordered_nodes)}
    block_positions = [
        (
            segment,
            resolve(segment.epub_state.block_path),
            node_positions[id(resolve(segment.epub_state.block_path))],
        )
        for segment in segments
        if segment.epub_state is not None
    ]

    def containing_segment(node: etree._Element) -> str | None:
        for segment, block, _block_index in block_positions:
            if node is block or any(node is child for child in block.iterdescendants()):
                top = node
                while top.getparent() is not block and top.getparent() is not None:
                    top = top.getparent()
                direct = element_children(block)
                run_index = (
                    0
                    if top is block
                    else sum(
                        1
                        for child in direct[: direct.index(top)]
                        if child.tag.rsplit("}", 1)[-1].lower() == "br"
                    )
                    if top in direct
                    else 0
                )
                return segment.anchor if run_index == 0 else f"{segment.anchor}_br{run_index}"
        return None

    fragment_anchors: dict[str, str | None] = {}
    for node_index, node in enumerate(ordered_nodes):
        if not isinstance(node.tag, str):
            continue
        identifiers = [value for value in (node.get("id"), node.get("name")) if value]
        if not identifiers:
            continue
        anchor = containing_segment(node)
        if anchor is None:
            later = [
                segment
                for segment, _block, block_index in block_positions
                if block_index > node_index
            ]
            anchor = later[0].anchor if later else None
        for identifier in identifiers:
            fragment_anchors.setdefault(identifier, anchor)
    return fragment_anchors


def _resource_title(
    root: etree._Element,
    href: str,
    book_title: str,
    *,
    protected_paths: Collection[tuple[int, ...]] = (),
) -> str:
    for heading in root.iter():
        if isinstance(heading.tag, str) and heading.tag.rsplit("}", 1)[-1].lower() in _HEADING_TAGS:
            title = visible_text(heading, protected_paths=protected_paths, root=root)
            if title:
                return title
    for title_node in root.iter():
        if isinstance(title_node.tag, str) and title_node.tag.rsplit("}", 1)[-1].lower() == "title":
            candidate = visible_text(title_node, protected_paths=protected_paths, root=root)
            return (
                candidate
                if candidate and not looks_like_internal_title(candidate, href, book_title)
                else ""
            )
    return ""


def annotate_resource(
    data: bytes,
    resource_index: int,
    href: str,
    *,
    tree: etree._ElementTree,
    parse_mode: str,
    diagnostics: list[dict[str, object]],
    protected_paths: Collection[tuple[int, ...]] = (),
    note_target_paths: Collection[tuple[int, ...]] = (),
    book_title: str = "",
    skip_navigation: bool = False,
) -> tuple[str, list[Segment], dict[str, object]]:
    root = tree.getroot()
    for element in root.iter():
        if (
            isinstance(element.tag, str)
            and element.tag.rsplit("}", 1)[-1].lower() == "aside"
            and element_path(root, element) in note_target_paths
            and any(
                isinstance(child.tag, str)
                and child.tag.rsplit("}", 1)[-1].lower() in _BLOCK_CANDIDATE_TAGS
                for child in element.iterdescendants()
            )
            and _note_aside_direct_text(
                root,
                element,
                protected_paths=protected_paths,
            )
        ):
            raise ValueError(
                f"EPUB note aside has mixed direct prose that cannot be extracted losslessly: {href}"
            )
    digest = hashlib.sha256(data).hexdigest()
    segments = _segments_for_blocks(
        root,
        lxml_targets(
            root,
            skip_navigation=skip_navigation,
            protected_paths=protected_paths,
            note_target_paths=note_target_paths,
        ),
        resource_index=resource_index,
        href=href,
        digest=digest,
        parse_mode=parse_mode,
        protected_paths=protected_paths,
    )
    return (
        _resource_title(
            root,
            href,
            book_title,
            protected_paths=protected_paths,
        ),
        segments,
        {
            "href": href,
            "index": resource_index,
            "resource_sha256": digest,
            "parse_mode": parse_mode,
            "parser_diagnostics": diagnostics,
            "fragment_anchors": _fragment_anchors(root, segments),
        },
    )
