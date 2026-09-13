"""Schema4 bilingual source insertion proofs."""

from __future__ import annotations

from typing import Any

from lxml import etree

from trans_novel.assemble.epub.rendering import (
    BILINGUAL_DIRECT_TARGET_ATTRS,
    direct_run_boundary,
    is_bilingual_container_tag,
    japanese_ruby_source_copy,
    sanitized_source_copy,
)
from trans_novel.assemble.epub.verification import archive_model, direct, dom, source

MAX_MEMBER_BYTES = archive_model.MAX_MEMBER_BYTES


def _structural_children(parent: etree._Element) -> list[etree._Element]:
    return [
        child
        for child in dom.element_children_lxml(parent)
        if (
            "tn-source" not in str(child.get("class", "")).split()
            and dict(child.attrib) != BILINGUAL_DIRECT_TARGET_ATTRS
        )
    ]


def _resolve_output_path(root: etree._Element, path: tuple[int, ...]) -> etree._Element | None:
    current = root
    for index in path:
        children = _structural_children(current)
        if index < 0 or index >= len(children):
            return None
        current = children[index]
    return current


def _validate_direct_runs(
    root_source: etree._Element,
    root_output: etree._Element,
    source_nodes: list[etree._Element],
    segments: list[Any],
    source_lang: str,
    order: str,
    resource: str,
    failures: list[dict[str, str]],
    direct_target_total: int,
) -> tuple[int, set[int]]:
    direct_target_used = 0
    direct_groups = source.direct_groups(root_source, segments)
    direct_source_paths: set[tuple[int, ...]] = set()
    direct_source_object_ids: set[int] = set()
    for block_path, direct_segments in direct_groups.items():
        source_block = dom.resolve_path_lxml(root_source, block_path)
        if source_block is None:
            continue
        target_block = _resolve_output_path(root_output, block_path)
        if target_block is None:
            failures.append(
                archive_model.item(
                    "bilingual_source", "source_target_pair_mismatch", resource, "pair_mismatch"
                )
            )
            continue
        block_direct_sources = [
            node
            for node in source_nodes
            if any(ancestor is target_block for ancestor in node.iterancestors())
        ]

        def resolve_owner(
            path: tuple[int, ...], target_block: etree._Element = target_block
        ) -> etree._Element | None:
            current = target_block
            for index in path:
                children = _structural_children(current)
                if index < 0 or index >= len(children):
                    return None
                current = children[index]
            return current

        def source_boundary(
            original_owner: etree._Element,
            source_block: etree._Element = source_block,
            block_path: tuple[int, ...] = block_path,
            target_block: etree._Element = target_block,
        ) -> etree._Element:
            original_boundary = direct_run_boundary(source_block, original_owner)
            boundary_path = dom.element_path_lxml(root_source, original_boundary)
            if boundary_path is None or boundary_path[: len(block_path)] != block_path:
                return target_block
            boundary = resolve_owner(tuple(boundary_path[len(block_path) :]))
            return boundary if boundary is not None else target_block

        run_plans = source.plan_direct_runs(
            root_source,
            target_block,
            source_block,
            block_path,
            direct_segments,
            root_output,
            resolve_owner,
            source_boundary,
        )
        direct_target_used, assigned = direct.match_direct_runs(
            root_source,
            root_output,
            source_nodes,
            source_block,
            target_block,
            block_path,
            direct_segments,
            run_plans,
            source_lang,
            order,
            resource,
            failures,
            direct_target_used,
            direct_source_paths,
            direct_source_object_ids,
        )
        if len(block_direct_sources) != len(assigned):
            failures.append(
                archive_model.item(
                    "bilingual_source", "source_node_order", resource, "pair_mismatch"
                )
            )
    if direct_target_used != direct_target_total:
        failures.append(
            archive_model.item("bilingual_source", "source_node_order", resource, "pair_mismatch")
        )
    return direct_target_used, direct_source_object_ids


def _check_generic_subtree(
    root_source: etree._Element,
    root_output: etree._Element,
    node: etree._Element,
    chosen: tuple[int, str, tuple[int, ...]],
    source_lang: str,
    resource: str,
    failures: list[dict[str, str]],
) -> None:
    source_block = dom.resolve_path_lxml(root_source, chosen[2])
    if source_block is None or (
        archive_model.local_name(node.tag).lower() == "span"
        and any(
            archive_model.local_name(descendant.tag).lower() == "br"
            for descendant in source_block.iter()
        )
    ):
        return
    expected_source = japanese_ruby_source_copy(source_block, source_lang, node.tag)
    if expected_source is None:
        expected_source = sanitized_source_copy(source_block, node.tag)
    if dom.source_subtree_signature(expected_source) != dom.source_subtree_signature(node):
        failures.append(
            archive_model.item(
                "bilingual_source", "source_node_subtree_mismatch", resource, "invalid"
            )
        )


def _validate_generic_sources(
    root_source: etree._Element,
    root_output: etree._Element,
    source_nodes: list[etree._Element],
    expected: list[tuple[str, tuple[int, ...]]],
    source_lang: str,
    resource: str,
    failures: list[dict[str, str]],
    node_parents: dict[int, etree._Element | None],
    node_siblings: dict[int, list[etree._Element]],
    node_text_context: dict[int, tuple[str | None, str | None]],
    node_mixed_context: dict[
        int, tuple[str | None, list[tuple[etree._Element, str | None, str | None]]]
    ],
    direct_source_object_ids: set[int],
    order: str,
) -> None:
    matched: set[int] = set()
    for node in source_nodes:
        if id(node) in direct_source_object_ids:
            continue
        text = dom.source_node_visible_text(node)
        candidates = [
            (index, source_text, block_path)
            for index, (source_text, block_path) in enumerate(expected)
            if index not in matched and dom.norm_text(source_text) == text
        ]
        chosen: tuple[int, str, tuple[int, ...]] | None = None
        for candidate in candidates:
            target = dom.resolve_path_lxml(root_output, candidate[2])
            parent = node_parents.get(id(node))
            if target is None or parent is None:
                continue
            if parent is target or parent is target.getparent():
                chosen = candidate
                break
        if chosen is None:
            failures.append(
                archive_model.item(
                    "bilingual_source", "source_target_pair_mismatch", resource, "pair_mismatch"
                )
            )
            continue
        matched.add(chosen[0])
        _check_generic_subtree(
            root_source, root_output, node, chosen, source_lang, resource, failures
        )
        target = dom.resolve_path_lxml(root_output, chosen[2])
        parent = node_parents.get(id(node))
        original_children = node_siblings.get(id(parent), []) if parent is not None else []
        if (
            target is None
            or parent is None
            or node not in original_children
            or (parent is not target and target not in original_children)
        ):
            continue
        node_index = original_children.index(node)
        _original_parent_text, _original_node_tail = node_text_context.get(id(node), (None, None))
        if parent is target and is_bilingual_container_tag(target.tag):
            mixed = node_mixed_context.get(id(parent))
            before_parts: list[str] = []
            after_parts: list[str] = []
            seen_source = False
            if mixed is not None:
                parent_text, entries = mixed
                before_parts.append(parent_text or "")
                for child, _child_text, child_tail in entries:
                    if child is node:
                        seen_source = True
                        after_parts.append(child_tail or "")
                        continue
                    visible = dom.source_node_visible_text(child)
                    if seen_source:
                        after_parts.extend((visible, child_tail or ""))
                    else:
                        before_parts.extend((visible, child_tail or ""))
            if (order == "target_first" and dom.norm_text("".join(after_parts))) or (
                order == "source_first" and dom.norm_text("".join(before_parts))
            ):
                failures.append(
                    archive_model.item(
                        "bilingual_source", "source_node_order", resource, "pair_mismatch"
                    )
                )
        elif parent is target.getparent():
            target_index = original_children.index(target)
            between = original_children[
                min(node_index, target_index) + 1 : max(node_index, target_index)
            ]
            between = [child for child in between if child not in source_nodes]
            if any(archive_model.local_name(child.tag).lower() != "br" for child in between):
                failures.append(
                    archive_model.item(
                        "bilingual_source", "source_node_misplaced", resource, "unattached"
                    )
                )
            if (node_index < target_index) != (order == "source_first"):
                failures.append(
                    archive_model.item(
                        "bilingual_source", "source_node_order", resource, "pair_mismatch"
                    )
                )


def bilingual_proof(
    root_source: etree._Element,
    root_output: etree._Element,
    segments: list[Any],
    *,
    source_lang: str,
    order: str,
    resource: str,
    failures: list[dict[str, str]],
) -> int:
    """Validate and remove only the current schema4 bilingual insertions."""
    source_nodes = [
        node
        for node in root_output.iter()
        if isinstance(node.tag, str) and "tn-source" in str(node.get("class", "")).split()
    ]
    expected, expected_total = source.expected_insertions(root_source, segments, source_lang)
    source.validate_source_nodes(root_output, source_nodes, resource, failures)
    (
        node_parents,
        node_siblings,
        node_text_context,
        node_mixed_context,
        direct_target_total,
    ) = source.node_snapshot(root_output, source_nodes)
    _direct_target_used, direct_source_object_ids = _validate_direct_runs(
        root_source,
        root_output,
        source_nodes,
        segments,
        source_lang,
        order,
        resource,
        failures,
        direct_target_total,
    )
    for node in list(source_nodes):
        source.remove_preserving_tail(node)
    for node in list(root_output.iter()):
        if dict(node.attrib) == BILINGUAL_DIRECT_TARGET_ATTRS and not len(node):
            source.unwrap_preserving_text(node)

    if len(source_nodes) != expected_total:
        failures.append(
            archive_model.item("bilingual_source", "source_node_count", resource, "count_mismatch")
        )
    _validate_generic_sources(
        root_source,
        root_output,
        source_nodes,
        expected,
        source_lang,
        resource,
        failures,
        node_parents,
        node_siblings,
        node_text_context,
        node_mixed_context,
        direct_source_object_ids,
        order,
    )

    return len(source_nodes)


def lang_attr(key: str) -> bool:
    return key in {"lang", "{http://www.w3.org/XML/1998/namespace}lang"}
