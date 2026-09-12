"""Render translated text back into source XHTML trees."""

from __future__ import annotations

import hashlib
from copy import deepcopy

from lxml import etree

from trans_novel.assemble.epub.metadata import translated_toc_title
from trans_novel.assemble.epub.rendering.bilingual import (
    BILINGUAL_DIRECT_TARGET_ATTRS,
    BILINGUAL_SOURCE_CLASS,
    direct_run_add_whitespace,
    direct_run_boundary,
    direct_run_is_active,
    direct_run_source_copy,
    has_reserved_source_collision,
    is_bilingual_container_tag,
    ruby_base_count,
    segment_needs_source,
)
from trans_novel.assemble.epub.rendering.source_dom import (
    bilingual_source_copy,
    effective_language,
    indexed_toc_entries,
    parse_source_markup,
    resolve_element_path,
    rewrite_markup_languages,
    rewrite_nav_labels,
    serialize_source_tree,
    set_visible_label,
)
from trans_novel.epub.slots import (
    normalized_source_text,
    normalized_target_text,
    slot_contract_digest,
)
from trans_novel.ingest import Segment

_HTML_EXTS = (".xhtml", ".html", ".htm")


def rewrite_toc_lxml(
    data: bytes,
    entries: list[dict[str, object]],
    *,
    is_ncx: bool,
    toc_path: str,
    target_lang: str,
    expected_mode: str | None = None,
    source_lang: str = "",
) -> bytes:
    tree, mode = parse_source_markup(data, expected_mode)
    root = tree.getroot()
    indexed = indexed_toc_entries(entries, toc_path)
    if is_ncx:
        _rewrite_ncx_labels(root, indexed, toc_path)
    else:
        rewrite_nav_labels(root, indexed, toc_path, source_lang)
        rewrite_markup_languages(root, target_lang)
    return serialize_source_tree(tree, data, mode)


def _rewrite_ncx_labels(
    root: etree._Element, indexed: dict[int, dict[str, object]], toc_path: str
) -> None:
    points = [
        node
        for node in root.iter()
        if isinstance(node.tag, str) and node.tag.rsplit("}", 1)[-1] == "navPoint"
    ]
    for node_index, point in enumerate(points):
        entry = indexed.get(node_index)
        if entry is None:
            continue
        content = next(
            (
                child
                for child in point.iter()
                if isinstance(child.tag, str) and child.tag.rsplit("}", 1)[-1] == "content"
            ),
            None,
        )
        expected = entry.get("raw_href")
        if isinstance(expected, str) and content is not None and content.get("src") != expected:
            raise ValueError(f"EPUB TOC href mismatch in {toc_path}")
        label = next(
            (
                node
                for node in point.iter()
                if isinstance(node.tag, str) and node.tag.rsplit("}", 1)[-1] == "text"
            ),
            None,
        )
        title = translated_toc_title(entry)
        if label is not None and title:
            set_visible_label(label, title)


def eligible_bilingual_segment(segment: Segment) -> bool:
    return segment_needs_source(segment)


def _group_bilingual_segments(segments: list[Segment]) -> dict[tuple[int, ...], list[Segment]]:
    grouped: dict[tuple[int, ...], list[Segment]] = {}
    for segment in segments:
        if not eligible_bilingual_segment(segment):
            continue
        state = segment.epub_state
        assert state is not None
        grouped.setdefault(state.block_path, []).append(segment)
    return grouped


def _add_container_source(
    block: etree._Element,
    original: etree._Element | None,
    segments: list[Segment],
    *,
    order: str,
    source_lang: str,
) -> int:
    namespace = block.nsmap.get(None)
    source_name = f"{{{namespace}}}div" if namespace else "div"
    source = (
        bilingual_source_copy(original, block, source_lang=source_lang, source_tag=source_name)
        if original is not None
        else etree.Element(source_name)
    )
    if original is None:
        source.text = segments[0].source
    source.set("class", BILINGUAL_SOURCE_CLASS)
    source.tail = None
    if order == "source_first":
        source.tail = block.text
        block.text = None
        block.insert(0, source)
    else:
        block.append(source)
    return 1


def _direct_owner_map(
    block: etree._Element, segments: list[Segment]
) -> dict[int, list[tuple[object, etree._Element, etree._Element]]]:
    owner_map: dict[int, list[tuple[object, etree._Element, etree._Element]]] = {}
    for segment in segments:
        state = segment.epub_state
        assert state is not None
        owner_map[id(segment)] = [
            (
                slot,
                resolve_element_path(block, slot.element_path),
                direct_run_boundary(block, resolve_element_path(block, slot.element_path)),
            )
            for slot in state.slots
        ]
    return owner_map


def _direct_pending_sources(
    block: etree._Element,
    original: etree._Element | None,
    segments: list[Segment],
    owner_map: dict[int, list[tuple[object, etree._Element, etree._Element]]],
    *,
    source_lang: str,
    span_name: str,
) -> list[tuple[object, etree._Element, etree._Element, etree._Element, etree._Element]]:
    pending: list[
        tuple[object, etree._Element, etree._Element, etree._Element, etree._Element]
    ] = []
    for segment in segments:
        state = segment.epub_state
        assert state is not None
        seen_rubies: set[int] = set()
        leading_whitespace = ""
        last_source: etree._Element | None = None
        for slot, owner, boundary in owner_map[id(segment)]:
            original_owner = (
                resolve_element_path(original, slot.element_path) if original is not None else owner
            )
            ruby = next(
                (
                    node
                    for node in (original_owner, *original_owner.iterancestors())
                    if node is not block
                    and isinstance(node.tag, str)
                    and node.tag.rsplit("}", 1)[-1].lower() == "ruby"
                ),
                None,
            )
            if ruby is not None and slot.field == "tail" and original_owner is ruby:
                ruby = None
            if ruby is not None and ruby_base_count(ruby) <= 1:
                ruby = None
            ruby_id = id(ruby) if ruby is not None else None
            if not slot.source_value.strip():
                if ruby_id is None:
                    if last_source is None:
                        leading_whitespace += slot.source_value
                    else:
                        direct_run_add_whitespace(last_source, suffix=slot.source_value)
                owner.text = None if slot.field == "text" else owner.text
                if slot.field == "tail":
                    owner.tail = None
                continue
            source = None
            if ruby_id is None or ruby_id not in seen_rubies:
                source = direct_run_source_copy(
                    original if original is not None else block,
                    original_owner,
                    source_lang=source_lang,
                    source_tag=span_name,
                    source_value=slot.source_value,
                    ruby_source=ruby_id is not None,
                )
                if leading_whitespace:
                    direct_run_add_whitespace(source, prefix=leading_whitespace)
                    leading_whitespace = ""
                if ruby_id is not None:
                    seen_rubies.add(ruby_id)
                last_source = source
            target = etree.Element(span_name, **BILINGUAL_DIRECT_TARGET_ATTRS)
            target.text = slot.target_value if slot.target_value is not None else slot.source_value
            if slot.field == "text":
                owner.text = None
                owner.insert(0, target)
            else:
                owner.tail = None
                parent = owner.getparent()
                if parent is None:
                    raise ValueError("EPUB direct-br tail slot has no parent")
                parent.insert(parent.index(owner) + 1, target)
            if source is not None:
                pending.append((slot, owner, boundary, source, target))
    return pending


def _insert_direct_sources(
    block: etree._Element,
    pending: list[tuple[object, etree._Element, etree._Element, etree._Element, etree._Element]],
    *,
    order: str,
) -> None:
    grouped: dict[int, list[tuple[object, etree._Element, etree._Element, etree._Element]]] = {}
    for slot, owner, boundary, source, target in pending:
        if (
            slot.field == "text"
            and boundary is owner
            and boundary is not block
            and not direct_run_is_active(boundary)
        ):
            owner.insert(0, source) if order == "source_first" else owner.insert(
                owner.index(target) + 1, source
            )
            continue
        if (
            slot.field == "tail"
            and boundary is owner
            and boundary is not block
            and not direct_run_is_active(boundary)
        ):
            parent = owner.getparent()
            if parent is None:
                raise ValueError("EPUB direct-br tail source has no parent")
            index = parent.index(target)
            parent.insert(index if order == "source_first" else index + 1, source)
            continue
        grouped.setdefault(id(boundary), []).append((slot, boundary, source, target))
    for entries in grouped.values():
        boundary = entries[0][1]
        parent = boundary if boundary is block else boundary.getparent()
        if parent is None:
            raise ValueError("EPUB direct-br source boundary has no parent")
        ordered = list(reversed(entries)) if order == "target_first" else entries
        for _slot, _boundary, source, target in ordered:
            if boundary is block:
                target_index = parent.index(target)
                parent.insert(target_index if order == "source_first" else target_index + 1, source)
            elif order == "source_first":
                boundary.addprevious(source)
            elif target.getparent() is parent:
                target.addnext(source)
            else:
                boundary.addnext(source)


def _add_direct_sources(
    root: etree._Element,
    block: etree._Element,
    original: etree._Element | None,
    segments: list[Segment],
    *,
    order: str,
    source_lang: str,
) -> int:
    namespace = block.nsmap.get(None)
    span_name = f"{{{namespace}}}span" if namespace else "span"
    owner_map = _direct_owner_map(block, segments)
    pending = _direct_pending_sources(
        block, original, segments, owner_map, source_lang=source_lang, span_name=span_name
    )
    _insert_direct_sources(block, pending, order=order)
    return len(pending)


def _add_plain_sources(
    block: etree._Element,
    original: etree._Element | None,
    segments: list[Segment],
    *,
    order: str,
    source_lang: str,
) -> int:
    sources: list[etree._Element] = []
    block_name = block.tag.rsplit("}", 1)[-1].lower()
    source_tag = block_name if block_name in {"p", "div"} else "p"
    for segment in segments:
        namespace = block.nsmap.get(None)
        source_name = f"{{{namespace}}}{source_tag}" if namespace else source_tag
        source = (
            bilingual_source_copy(original, block, source_lang=source_lang, source_tag=source_name)
            if original is not None and len(segments) == 1
            else etree.Element(source_name)
        )
        if original is None or len(segments) != 1:
            source.text = segment.source
        source.set("class", BILINGUAL_SOURCE_CLASS)
        source.tail = None
        sources.append(source)
    if order == "source_first":
        for source in reversed(sources):
            block.addprevious(source)
    else:
        old_tail = block.tail
        block.tail = None
        anchor = block
        for index, source in enumerate(sources):
            if index == len(sources) - 1:
                source.tail = old_tail
            anchor.addnext(source)
            anchor = source
    return len(sources)


def add_bilingual_sources(
    root: etree._Element,
    segments: list[Segment],
    *,
    order: str = "target_first",
    source_lang: str = "",
    source_blocks: dict[tuple[int, ...], etree._Element] | None = None,
    block_refs: dict[tuple[int, ...], etree._Element] | None = None,
) -> int:
    grouped = _group_bilingual_segments(segments)
    added = 0
    for block_path, block_segments in grouped.items():
        block = block_refs.get(block_path) if block_refs else None
        if block is None:
            block = resolve_element_path(root, block_path)
        original = source_blocks.get(block_path) if source_blocks else None
        tag = block.tag if isinstance(block.tag, str) else "p"
        if is_bilingual_container_tag(tag):
            added += _add_container_source(
                block, original, block_segments, order=order, source_lang=source_lang
            )
            continue
        direct_br = any(
            isinstance(child.tag, str) and child.tag.rsplit("}", 1)[-1].lower() == "br"
            for child in (original if original is not None else block)
        )
        if direct_br:
            added += _add_direct_sources(
                root, block, original, block_segments, order=order, source_lang=source_lang
            )
        else:
            added += _add_plain_sources(
                block, original, block_segments, order=order, source_lang=source_lang
            )
    return added


def _apply_declared_languages(
    root: etree._Element,
    segments: list[Segment],
    *,
    source_lang: str,
    target_lang: str,
    href: str,
    source_languages: dict[tuple[int, ...], str | None],
    block_refs: dict[tuple[int, ...], etree._Element],
) -> None:
    if segments and all(segment.preserve_source for segment in segments):
        return
    rewrite_markup_languages(root, target_lang)
    by_block: dict[tuple[int, ...], set[bool]] = {}
    for segment in segments:
        state = segment.epub_state
        assert state is not None
        by_block.setdefault(state.block_path, set()).add(segment.preserve_source)
    for block_path, decisions in by_block.items():
        if len(decisions) > 1:
            raise ValueError(f"EPUB preserve range is ambiguous within one block: {href}")
        if decisions != {True}:
            continue
        expected = source_languages[block_path]
        block = block_refs[block_path]
        if expected is not None and effective_language(block) != expected:
            block.set("{http://www.w3.org/XML/1998/namespace}lang", expected)


def render_source_resource(
    data: bytes,
    href: str,
    segments: list[Segment],
    *,
    expected_digest: str,
    expected_mode: str,
    target_lang: str,
    bilingual: bool = False,
    order: str = "target_first",
    source_lang: str = "",
) -> bytes:
    if order not in {"target_first", "source_first"}:
        raise ValueError(f"invalid bilingual order: {order!r}")
    actual_digest = hashlib.sha256(data).hexdigest()
    if actual_digest != expected_digest:
        raise ValueError(f"EPUB resource digest mismatch: {href}")
    tree, mode = parse_source_markup(data, expected_mode)
    root = tree.getroot()
    if bilingual and has_reserved_source_collision(root):
        raise ValueError(f"EPUB reserved bilingual marker collision: {href}")
    writes: list[tuple[etree._Element, str, str]] = []
    source_blocks: dict[tuple[int, ...], etree._Element] = {}
    block_refs: dict[tuple[int, ...], etree._Element] = {}
    source_languages: dict[tuple[int, ...], str | None] = {}
    for segment in segments:
        state = segment.epub_state
        if state is None:
            raise ValueError(f"EPUB segment missing slot state: {href}")
        if state.resource_href != href or state.resource_sha256 != actual_digest:
            raise ValueError(f"EPUB segment resource contract mismatch: {href}")
        if state.slot_contract_sha256 != slot_contract_digest(state.slots):
            raise ValueError(f"EPUB slot contract digest mismatch: {href}")
        block = resolve_element_path(root, state.block_path)
        block_refs.setdefault(state.block_path, block)
        source_languages.setdefault(
            state.block_path, effective_language(block, source_lang or None)
        )
        if (bilingual or segment.preserve_source) and state.block_path not in source_blocks:
            source_blocks[state.block_path] = deepcopy(block)
        expected_fingerprint = hashlib.sha256(
            etree.tostring(block, encoding="utf-8", with_tail=False)
        ).hexdigest()
        if expected_fingerprint != state.block_fingerprint:
            raise ValueError(f"EPUB block fingerprint mismatch: {href}")
        if segment.source != normalized_source_text(state.slots):
            raise ValueError(f"EPUB segment source derivation mismatch: {href}")
        assigned = all(slot.target_value is not None for slot in state.slots)
        if segment.target is None:
            if any(slot.target_value is not None for slot in state.slots):
                raise ValueError(f"EPUB segment target derivation mismatch: {href}")
        elif not assigned or segment.target != normalized_target_text(state.slots):
            raise ValueError(f"EPUB segment target derivation mismatch: {href}")
        for slot in state.slots:
            owner = resolve_element_path(block, slot.element_path)
            value = owner.text if slot.field == "text" else owner.tail
            if value != slot.source_value:
                raise ValueError(f"EPUB slot source mismatch: {href}")
            replacement = (
                slot.source_value
                if segment.preserve_source
                else slot.target_value
                if slot.target_value is not None
                else slot.source_value
            )
            writes.append((owner, slot.field, replacement))
    for owner, field, replacement in writes:
        if field == "text":
            owner.text = replacement
        else:
            owner.tail = replacement
    _apply_declared_languages(
        root,
        segments,
        source_lang=source_lang,
        target_lang=target_lang,
        href=href,
        block_refs=block_refs,
        source_languages=source_languages,
    )
    if bilingual:
        added = add_bilingual_sources(
            root,
            segments,
            order=order,
            source_lang=source_lang,
            source_blocks=source_blocks,
            block_refs=block_refs,
        )
        if added:
            from trans_novel.assemble.epub.rendering.bilingual import append_bilingual_style

            append_bilingual_style(root)
    return serialize_source_tree(tree, data, mode)
