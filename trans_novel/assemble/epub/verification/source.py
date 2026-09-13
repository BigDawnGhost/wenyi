"""Source-resource evidence stages for schema4 bilingual proofs."""

from __future__ import annotations

from collections import Counter
from typing import Any

from lxml import etree

from trans_novel.assemble.epub.rendering import (
    BILINGUAL_DIRECT_TARGET_ATTRS,
    BILINGUAL_SOURCE_CLASS,
    is_bilingual_container_tag,
    japanese_ruby_source_copy,
    ruby_base_count,
    sanitized_source_copy,
    segment_needs_source,
    source_node_is_valid,
)
from trans_novel.assemble.epub.verification import archive_model, dom


def expected_insertions(root_source: etree._Element, segments: list[Any], source_lang: str):
    expected: list[tuple[str, tuple[int, ...]]] = []
    expected_total = 0
    container_paths: set[tuple[int, ...]] = set()
    direct_source_keys: set[tuple[tuple[int, ...], tuple[int, ...] | str]] = set()
    block_counts = Counter(
        tuple(segment.epub_state.block_path)
        for segment in segments
        if segment_needs_source(segment) and segment.epub_state is not None
    )
    for segment in segments:
        if not segment_needs_source(segment):
            continue
        state = segment.epub_state
        assert state is not None
        block_path = tuple(state.block_path)
        source_block = dom.resolve_path_lxml(root_source, block_path)
        if source_block is not None and is_bilingual_container_tag(source_block.tag):
            if block_path not in container_paths:
                container_paths.add(block_path)
                expected_source = japanese_ruby_source_copy(
                    source_block, source_lang, "div"
                ) or sanitized_source_copy(source_block, "div")
                expected.append((dom.source_node_visible_text(expected_source), block_path))
                expected_total += 1
            continue
        direct_segment = source_block is not None and any(
            isinstance(child.tag, str) and archive_model.local_name(child.tag).lower() == "br"
            for child in source_block
        )
        if direct_segment:
            for slot_index, slot in enumerate(state.slots):
                if not slot.source_value.strip():
                    continue
                owner = dom.resolve_path_lxml(source_block, tuple(slot.element_path))
                ruby = (
                    next(
                        (
                            node
                            for node in (owner, *owner.iterancestors())
                            if node is not source_block
                            and isinstance(node.tag, str)
                            and archive_model.local_name(node.tag) == "ruby"
                        ),
                        None,
                    )
                    if owner is not None
                    else None
                )
                if ruby is not None and slot.field == "tail" and owner is ruby:
                    ruby = None
                if ruby is not None and ruby_base_count(ruby) <= 1:
                    ruby = None
                ruby_path = dom.element_path_lxml(root_source, ruby) if ruby is not None else None
                key = (
                    block_path,
                    ruby_path or ("slot", getattr(slot, "id", f"{id(segment)}:{slot_index}")),
                )
                if key not in direct_source_keys:
                    direct_source_keys.add(key)
                    expected_total += 1
        else:
            source_text = segment.source
            if source_block is not None and block_counts[block_path] == 1:
                source_tag = archive_model.local_name(source_block.tag).lower()
                source_tag = source_tag if source_tag in {"p", "div"} else "p"
                expected_source = japanese_ruby_source_copy(source_block, source_lang, source_tag)
                if expected_source is None:
                    expected_source = sanitized_source_copy(source_block, source_tag)
                source_text = dom.source_node_visible_text(expected_source)
            expected.append((source_text, block_path))
            expected_total += 1
    return expected, expected_total


def validate_source_nodes(
    root_output: etree._Element,
    source_nodes: list[etree._Element],
    resource: str,
    failures: list[dict[str, str]],
) -> None:
    for node in source_nodes:
        attrs = dict(node.attrib)
        if not source_node_is_valid(node) or attrs != {"class": BILINGUAL_SOURCE_CLASS}:
            failures.append(
                archive_model.item(
                    "bilingual_source", "source_node_attributes", resource, "invalid"
                )
            )
        if node.tag.rsplit("}", 1)[-1].lower() not in {"p", "div", "span"}:
            failures.append(
                archive_model.item("bilingual_source", "source_node_shape", resource, "invalid")
            )
        for child in node.iter():
            if not isinstance(child.tag, str):
                continue
            name = child.tag.rsplit("}", 1)[-1].lower()
            if name in {
                "audio",
                "canvas",
                "embed",
                "iframe",
                "img",
                "object",
                "script",
                "source",
                "style",
                "svg",
                "video",
            }:
                failures.append(
                    archive_model.item(
                        "bilingual_source", "source_node_active_media", resource, "media"
                    )
                )
            if any(
                key.rsplit("}", 1)[-1].lower() in {"id", "name", "href", "src"}
                or key.lower().startswith("on")
                for key in child.attrib
            ):
                failures.append(
                    archive_model.item(
                        "bilingual_source", "source_node_active_attribute", resource, "media"
                    )
                )


def node_snapshot(root_output: etree._Element, source_nodes: list[etree._Element]):
    node_parents = {id(node): node.getparent() for node in source_nodes}
    node_siblings = {
        id(parent): dom.element_children_lxml(parent)
        for parent in node_parents.values()
        if parent is not None
    }
    node_text_context = {
        id(node): ((node.getparent().text if node.getparent() is not None else None), node.tail)
        for node in source_nodes
    }
    node_mixed_context: dict[
        int, tuple[str | None, list[tuple[etree._Element, str | None, str | None]]]
    ] = {}
    for parent in node_parents.values():
        if parent is None or id(parent) in node_mixed_context:
            continue
        node_mixed_context[id(parent)] = (
            parent.text,
            [(child, child.text, child.tail) for child in dom.element_children_lxml(parent)],
        )
    direct_target_total = sum(
        1
        for node in root_output.iter()
        if isinstance(node.tag, str) and dict(node.attrib) == BILINGUAL_DIRECT_TARGET_ATTRS
    )
    return (
        node_parents,
        node_siblings,
        node_text_context,
        node_mixed_context,
        direct_target_total,
    )


def remove_preserving_tail(node: etree._Element) -> None:
    parent = node.getparent()
    if parent is None:
        return
    tail = node.tail
    previous = node.getprevious()
    if tail:
        if previous is not None:
            previous.tail = (previous.tail or "") + tail
        else:
            parent.text = (parent.text or "") + tail
    parent.remove(node)


def unwrap_preserving_text(node: etree._Element) -> None:
    parent = node.getparent()
    if parent is None:
        return
    previous = node.getprevious()
    text = (node.text or "") + (node.tail or "")
    if previous is not None:
        previous.tail = (previous.tail or "") + text
    else:
        parent.text = (parent.text or "") + text
    parent.remove(node)


def direct_groups(root_source: etree._Element, segments: list[Any]):
    groups: dict[tuple[int, ...], list[Any]] = {}
    for segment in segments:
        if not segment_needs_source(segment):
            continue
        state = segment.epub_state
        assert state is not None
        source_block = dom.resolve_path_lxml(root_source, tuple(state.block_path))
        if (
            source_block is None
            or is_bilingual_container_tag(source_block.tag)
            or not any(
                isinstance(child.tag, str) and archive_model.local_name(child.tag).lower() == "br"
                for child in source_block
            )
        ):
            continue
        groups.setdefault(tuple(state.block_path), []).append(segment)
    return groups


def plan_direct_runs(
    root_source: etree._Element,
    target_block: etree._Element,
    source_block: etree._Element,
    block_path: tuple[int, ...],
    direct_segments: list[Any],
    root_output: etree._Element,
    resolve_owner,
    source_boundary,
):
    run_plans: dict[
        int, tuple[list[tuple[Any, ...]], list[int], dict[int, str], dict[int, str]]
    ] = {}
    for segment in direct_segments:
        state = segment.epub_state
        assert state is not None
        records: list[tuple[Any, ...]] = []
        processed_rubies: set[tuple[int, ...]] = set()
        source_wrapper_slots: list[int] = []
        source_prefixes: dict[int, str] = {}
        source_suffixes: dict[int, str] = {}
        leading_whitespace = ""
        for slot_index, slot in enumerate(state.slots):
            owner = resolve_owner(tuple(slot.element_path))
            original_owner = dom.resolve_path_lxml(
                root_source,
                (*block_path, *slot.element_path),
            )
            ruby = (
                next(
                    (
                        node
                        for node in (original_owner, *original_owner.iterancestors())
                        if node is not source_block
                        and isinstance(node.tag, str)
                        and archive_model.local_name(node.tag) == "ruby"
                    ),
                    None,
                )
                if original_owner is not None
                else None
            )
            if ruby is not None and slot.field == "tail" and original_owner is ruby:
                ruby = None
            if ruby is not None and ruby_base_count(ruby) <= 1:
                ruby = None
            ruby_path = dom.element_path_lxml(root_source, ruby) if ruby is not None else None
            grouped_ruby = ruby_path is not None
            source_duplicate = grouped_ruby and ruby_path in processed_rubies
            boundary = source_boundary(original_owner) if original_owner is not None else None
            records.append(
                (
                    slot_index,
                    slot,
                    owner,
                    original_owner,
                    ruby_path,
                    grouped_ruby,
                    source_duplicate,
                    boundary,
                )
            )
            if not slot.source_value.strip():
                if grouped_ruby:
                    continue
                if source_wrapper_slots:
                    previous = source_wrapper_slots[-1]
                    source_suffixes[previous] += slot.source_value
                else:
                    leading_whitespace += slot.source_value
                continue
            if not source_duplicate:
                if grouped_ruby:
                    processed_rubies.add(ruby_path)
                source_wrapper_slots.append(slot_index)
                source_prefixes[slot_index] = leading_whitespace
                source_suffixes[slot_index] = ""
                leading_whitespace = ""
        run_plans[id(segment)] = (
            records,
            source_wrapper_slots,
            source_prefixes,
            source_suffixes,
        )
    return run_plans
