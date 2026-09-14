from __future__ import annotations

from collections.abc import Collection, Mapping
from copy import deepcopy
from dataclasses import replace
from typing import Literal, cast

from lxml import etree

from trans_novel.assemble.epub.rendering.source_dom import resolve_element_path
from trans_novel.assemble.epub.rendering.theme.contracts import (
    ElementPath,
    NoteAttributeChange,
    NoteChange,
    NoteNamespaceChange,
    NotePathMapping,
    NoteTextChange,
    ResourceThemeScope,
    ThemeError,
)
from trans_novel.epub.notes import NoteRelations

_EPUB_NS = "http://www.idpf.org/2007/ops"
_EPUB_TYPE = f"{{{_EPUB_NS}}}type"
_NOTE_KINDS = frozenset({"noteref", "backlink", "footnote", "endnote"})
_ROLE = {
    "noteref": "doc-noteref",
    "backlink": "doc-backlink",
    "footnote": "doc-footnote",
    "endnote": "doc-endnote",
}
_ARIA = {"noteref": "注释", "backlink": "返回正文"}


def _invalid(resource: str) -> ThemeError:
    return ThemeError("theme_verify", "invalid_plan", resource=resource)


def _local_name(node: etree._Element) -> str:
    return node.tag.rsplit("}", 1)[-1].lower() if isinstance(node.tag, str) else ""


def _inside_path(path: ElementPath, ancestor: ElementPath) -> bool:
    return len(path) >= len(ancestor) and path[: len(ancestor)] == ancestor


def _mapping(scope: ResourceThemeScope, resource: str) -> dict[ElementPath, ElementPath]:
    result: dict[ElementPath, ElementPath] = {}
    targets: set[ElementPath] = set()
    for item in scope.note_paths:
        if (
            not isinstance(item, NotePathMapping)
            or any(
                not isinstance(index, int) or isinstance(index, bool) or index < 0
                for path in (item.source_path, item.target_path)
                for index in path
            )
            or item.source_path in result
            or item.target_path in targets
        ):
            raise _invalid(resource)
        result[item.source_path] = item.target_path
        targets.add(item.target_path)
    return result


def _expected_source_paths(relations: NoteRelations, resource: str) -> tuple[ElementPath, ...]:
    paths: list[ElementPath] = []
    for marker in relations["markers"]:
        if marker["resource_href"] == resource:
            paths.append(tuple(marker["path"]))
    for target in relations["targets"]:
        if target["resource_href"] == resource:
            paths.append(tuple(target["path"]))
    return tuple(dict.fromkeys(paths))


def _validate_mapping_shape(
    source_root: etree._Element,
    rendered_root: etree._Element,
    resource: str,
    scope: ResourceThemeScope,
    expected_paths: Collection[ElementPath],
) -> dict[ElementPath, ElementPath]:
    mapping = _mapping(scope, resource)
    if set(mapping) != set(expected_paths):
        raise _invalid(resource)
    source_clone_paths = tuple(pair.source_path for pair in scope.source_pairs)
    for source_path, target_path in mapping.items():
        if any(_inside_path(target_path, clone) for clone in source_clone_paths):
            raise _invalid(resource)
        source = resolve_element_path(source_root, source_path)
        target = resolve_element_path(rendered_root, target_path)
        if _local_name(source) != _local_name(target):
            raise _invalid(resource)
        for name in ("id", "name", "href"):
            if source.get(name) != target.get(name):
                raise _invalid(resource)
    return mapping


def _attribute_changes(
    node: etree._Element,
    path: ElementPath,
    kind: Literal["noteref", "backlink", "footnote", "endnote"],
) -> tuple[NoteAttributeChange, ...] | None:
    role = node.get("role")
    if role not in {None, _ROLE[kind]}:
        return None
    before_type = node.get(_EPUB_TYPE)
    tokens = str(before_type or "").split()
    lowered = {token.lower() for token in tokens}
    if (_NOTE_KINDS & lowered) - {kind}:
        return None
    changes: list[NoteAttributeChange] = []
    if kind not in tokens:
        changes.append(
            NoteAttributeChange(
                path,
                _EPUB_TYPE,
                before_type,
                " ".join((*tokens, kind)),
            )
        )
    if role is None:
        changes.append(NoteAttributeChange(path, "role", None, _ROLE[kind]))
    aria = _ARIA.get(kind)
    if aria is not None and node.get("aria-label") != aria:
        changes.append(NoteAttributeChange(path, "aria-label", node.get("aria-label"), aria))
    if kind in {"noteref", "backlink"} and node.get("data-tn-note-kind") != kind:
        changes.append(
            NoteAttributeChange(path, "data-tn-note-kind", node.get("data-tn-note-kind"), kind)
        )
    return tuple(changes)


def _owned_text_fields(
    root: etree._Element, node: etree._Element
) -> list[tuple[ElementPath, str, str | None]]:
    paths: dict[etree._Element, ElementPath] = {}
    stack = [(root, ())]
    while stack:
        current, path = stack.pop()
        paths[current] = path
        children = [child for child in current if isinstance(child.tag, str)]
        stack.extend(
            (child, (*path, index)) for index, child in reversed(tuple(enumerate(children)))
        )
    fields: list[tuple[ElementPath, str, str | None]] = [(paths[node], "text", node.text)]
    for descendant in node.iterdescendants():
        if not isinstance(descendant.tag, str):
            continue
        fields.append((paths[descendant], "text", descendant.text))
        fields.append((paths[descendant], "tail", descendant.tail))
    return fields


def _text_changes(
    root: etree._Element,
    node: etree._Element,
    label: str,
    resource: str,
) -> tuple[NoteTextChange, ...]:
    fields = _owned_text_fields(root, node)
    if "".join(value or "" for _path, _field, value in fields) != label:
        raise _invalid(resource)
    owned = [(path, field, value) for path, field, value in fields if value is not None]
    if not owned:
        raise _invalid(resource)
    return tuple(
        NoteTextChange(path, field, value, "注" if index == 0 else "")
        for index, (path, field, value) in enumerate(owned)
    )


def _entries(
    relations: NoteRelations, resource: str
) -> tuple[tuple[Mapping[str, object], bool], ...]:
    return (
        *((marker, True) for marker in relations["markers"] if marker["resource_href"] == resource),
        *(
            (target, False)
            for target in relations["targets"]
            if target["resource_href"] == resource
        ),
    )


def _epub_prefix(node: etree._Element) -> str:
    used = {
        prefix for descendant in node.iter() for prefix in descendant.nsmap if prefix is not None
    }
    prefix = "epub"
    index = 1
    while prefix in used:
        prefix = f"epub{index}"
        index += 1
    return prefix


def _namespace_changes(
    root: etree._Element,
    path: ElementPath,
    attributes: Collection[NoteAttributeChange],
) -> tuple[NoteNamespaceChange, ...]:
    node = resolve_element_path(root, path)
    type_change = next(
        (change for change in attributes if change.name == _EPUB_TYPE),
        None,
    )
    if type_change is None or _EPUB_TYPE.rsplit("}", 1)[0][1:] in node.nsmap.values():
        return ()
    before = tuple(node.nsmap.items())
    probe_root = deepcopy(root)
    probe = resolve_element_path(probe_root, path)
    assert type_change.after is not None
    etree.register_namespace(_epub_prefix(node), _EPUB_NS)
    probe.set(_EPUB_TYPE, type_change.after)
    after = tuple(probe.nsmap.items())
    if before == after:
        return ()
    return (NoteNamespaceChange(path, before, after),)


def _changes_from_mapping(
    rendered_root: etree._Element,
    resource: str,
    mapping: Mapping[ElementPath, ElementPath],
    relations: NoteRelations,
    excluded_paths: Collection[ElementPath] = (),
) -> tuple[NoteChange, ...]:
    working_root = deepcopy(rendered_root)
    changes: list[NoteChange] = []
    for item, is_marker in _entries(relations, resource):
        source_path = tuple(cast(list[int], item["path"]))
        target_path = mapping[source_path]
        if any(_inside_path(target_path, excluded) for excluded in excluded_paths):
            continue
        target = resolve_element_path(working_root, target_path)
        kind = cast(Literal["noteref", "backlink", "footnote", "endnote"], item["kind"])
        if is_marker and _local_name(target) != "a":
            raise _invalid(resource)
        attributes = _attribute_changes(target, target_path, kind)
        if attributes is None:
            continue
        text = (
            _text_changes(working_root, target, cast(str, item["label"]), resource)
            if is_marker
            else ()
        )
        if text or attributes:
            namespaces = _namespace_changes(working_root, target_path, attributes)
            change = NoteChange(
                source_path,
                target_path,
                kind,
                text,
                attributes,
                namespaces,
            )
            apply_note_changes(working_root, (change,), resource=resource)
            changes.append(change)
    return tuple(changes)


def plan_note_markers(
    root: etree._Element,
    resource: str,
    scope: ResourceThemeScope,
    relations: NoteRelations | None,
    markers: dict[etree._Element, dict[str, str]],
) -> tuple[NoteChange, ...]:
    changes = plan_note_changes(root, resource, scope, relations) if relations is not None else ()
    for change in changes:
        if change.kind not in {"noteref", "backlink"}:
            continue
        node = resolve_element_path(root, change.target_path)
        attrs = markers.setdefault(node, {})
        attrs["data-tn-content"] = "target"
        attrs["data-tn-note-kind"] = change.kind
        if change.kind == "noteref":
            attrs["data-tn-role"] = "noteref"
    return changes


def source_backed_note_relations(
    enabled: bool,
    relations: NoteRelations | None,
    source_sha256: str | None,
) -> NoteRelations | None:
    if (
        enabled
        and relations is not None
        and bool(relations["markers"] or relations["targets"])
        and isinstance(source_sha256, str)
        and len(source_sha256) == 64
        and all(character in "0123456789abcdef" for character in source_sha256)
    ):
        return relations
    return None


def with_identity_note_paths(
    scope: ResourceThemeScope,
    relations: NoteRelations,
    resource: str,
) -> ResourceThemeScope:
    paths = tuple(
        tuple(cast(list[int], item["path"]))
        for item in (*relations["markers"], *relations["targets"])
        if item["resource_href"] == resource
    )
    return replace(
        scope,
        note_paths=tuple(
            NotePathMapping(source_path=path, target_path=path) for path in dict.fromkeys(paths)
        ),
    )


def plan_note_changes(
    rendered_root: etree._Element,
    resource: str,
    scope: ResourceThemeScope,
    relations: NoteRelations,
) -> tuple[NoteChange, ...]:
    expected_paths = _expected_source_paths(relations, resource)
    mapping = _mapping(scope, resource)
    if set(mapping) != set(expected_paths):
        raise _invalid(resource)
    if any(
        any(_inside_path(path, pair.source_path) for pair in scope.source_pairs)
        for path in mapping.values()
    ):
        raise _invalid(resource)
    return _changes_from_mapping(
        rendered_root,
        resource,
        mapping,
        relations,
        excluded_paths=scope.excluded_paths,
    )


def expected_note_changes(
    source_root: etree._Element,
    rendered_root: etree._Element,
    resource: str,
    scope: ResourceThemeScope,
    relations: NoteRelations,
) -> tuple[NoteChange, ...]:
    expected_paths = _expected_source_paths(relations, resource)
    mapping = _validate_mapping_shape(source_root, rendered_root, resource, scope, expected_paths)
    for item, is_marker in _entries(relations, resource):
        source_path = tuple(cast(list[int], item["path"]))
        source = resolve_element_path(source_root, source_path)
        target = resolve_element_path(rendered_root, mapping[source_path])
        if is_marker and (
            _local_name(source) != "a"
            or target.get("href") != source.get("href")
            or "".join(source.itertext()) != item["label"]
        ):
            raise _invalid(resource)
    return _changes_from_mapping(
        rendered_root,
        resource,
        mapping,
        relations,
        excluded_paths=scope.excluded_paths,
    )


def apply_note_changes(
    root: etree._Element,
    changes: Collection[NoteChange],
    *,
    reverse: bool = False,
    resource: str,
) -> None:
    ordered = reversed(tuple(changes)) if reverse else changes
    for change in ordered:
        namespace_changes = tuple(change.namespace_changes)
        expected_namespaces = "after" if reverse else "before"
        for item in namespace_changes:
            node = resolve_element_path(root, item.path)
            if tuple(node.nsmap.items()) != getattr(item, expected_namespaces):
                raise _invalid(resource)
        text_changes = reversed(change.text_changes) if reverse else change.text_changes
        for item in text_changes:
            node = resolve_element_path(root, item.path)
            expected = item.after if reverse else item.before
            replacement = item.before if reverse else item.after
            if item.field not in {"text", "tail"} or getattr(node, item.field) != expected:
                raise _invalid(resource)
            setattr(node, item.field, replacement)
        if not reverse:
            for namespace in namespace_changes:
                for prefix, uri in namespace.after:
                    if prefix is not None and (prefix, uri) not in namespace.before:
                        etree.register_namespace(prefix, uri)
        attribute_changes = (
            reversed(change.attribute_changes) if reverse else change.attribute_changes
        )
        for item in attribute_changes:
            node = resolve_element_path(root, item.path)
            expected = item.after if reverse else item.before
            replacement = item.before if reverse else item.after
            if node.get(item.name) != expected:
                raise _invalid(resource)
            if replacement is None:
                node.attrib.pop(item.name, None)
            else:
                node.set(item.name, replacement)
        for item in namespace_changes:
            node = resolve_element_path(root, item.path)
            if reverse:
                added = {prefix for prefix, _uri in item.after} - {
                    prefix for prefix, _uri in item.before
                }
                keep = {
                    prefix
                    for descendant in node.iter()
                    for prefix in descendant.nsmap
                    if prefix is not None and prefix not in added
                }
                etree.cleanup_namespaces(node, keep_ns_prefixes=tuple(keep))
            expected = item.before if reverse else item.after
            if tuple(node.nsmap.items()) != expected:
                raise _invalid(resource)
