from __future__ import annotations

import hashlib
import posixpath
import re
import zipfile
from collections import Counter
from collections.abc import Sequence
from urllib.parse import quote

import tinycss2
from bs4 import BeautifulSoup
from bs4.element import Tag
from lxml import etree

from trans_novel.assemble.epub.rendering.bilingual import (
    BILINGUAL_DIRECT_TARGET_CLASS,
    XHTML_NS,
    local_name,
)
from trans_novel.assemble.epub.rendering.source_dom import (
    element_children_lxml,
    parse_source_markup,
    resolve_element_path,
)
from trans_novel.assemble.epub.rendering.theme.cascade import (
    normalize_inline,
    source_specificity_bound,
)
from trans_novel.assemble.epub.rendering.theme.contracts import (
    ElementPath,
    InlineChange,
    LayoutBinding,
    MarkerChange,
    ResourceThemePlan,
    ResourceThemeScope,
    RoleAssignment,
    SourcePair,
    ThemeBundle,
    ThemeError,
)
from trans_novel.assemble.epub.rendering.theme.css import CssRule, declaration_properties
from trans_novel.assemble.epub.rendering.theme.notes import (
    plan_note_changes,
    plan_note_markers,
)
from trans_novel.assemble.epub.rendering.theme.projection import (
    ResourceProjection,
    build_projection,
)
from trans_novel.assemble.epub.rendering.theme.source_css import collect_source_stylesheets
from trans_novel.epub.layout import LayoutAssignment
from trans_novel.epub.notes import NoteRelations

_RESERVED_ATTRIBUTES = (
    "data-tn-role",
    "data-tn-level",
    "data-tn-content",
    "data-tn-theme-node",
)
_RESET_BARRIERS = {"small", "sup", "sub", "ruby", "rt", "rp", "rtc"}
_INHERITED_RESETS = {"font-family", "font-size"}


def tree_sha256(tree: etree._ElementTree, *, resource: str | None = None) -> str:
    try:
        data = etree.tostring(tree, method="c14n", with_comments=True)
    except (etree.LxmlError, TypeError, ValueError):
        raise ThemeError("theme_css", "invalid_markup", resource=resource) from None
    return hashlib.sha256(data).hexdigest()


def _fail_scope(resource: str) -> ThemeError:
    return ThemeError("theme_css", "invalid_scope", resource=resource)


def _namespace(node: etree._Element) -> str:
    tag = node.tag
    return tag[1:].split("}", 1)[0] if isinstance(tag, str) and tag.startswith("{") else ""


def _body(root: etree._Element, resource: str) -> etree._Element:
    bodies = [
        node
        for node in root.iter()
        if isinstance(node.tag, str)
        and local_name(node.tag) == "body"
        and _namespace(node) in {"", XHTML_NS}
    ]
    if len(bodies) != 1:
        raise ThemeError("theme_css", "invalid_markup", resource=resource)
    return bodies[0]


def _inside(node: etree._Element, ancestor: etree._Element) -> bool:
    return node is ancestor or ancestor in node.iterancestors()


def _valid_path(path: object) -> bool:
    return isinstance(path, tuple) and all(
        isinstance(index, int) and not isinstance(index, bool) and index >= 0 for index in path
    )


def _resolve_scope(
    root: etree._Element, scope: ResourceThemeScope, resource: str
) -> tuple[list[etree._Element], list[tuple[etree._Element, tuple[etree._Element, ...], bool]]]:
    body = _body(root, resource)
    if (
        not isinstance(scope, ResourceThemeScope)
        or not isinstance(scope.preserve_resource, bool)
        or any(not _valid_path(path) for path in scope.excluded_paths)
    ):
        raise _fail_scope(resource)
    try:
        excluded = [resolve_element_path(root, path) for path in scope.excluded_paths]
    except (ValueError, IndexError):
        raise _fail_scope(resource) from None
    if any(not _inside(node, body) for node in excluded):
        raise _fail_scope(resource)

    source_paths: set[ElementPath] = set()
    pairs: list[tuple[etree._Element, tuple[etree._Element, ...], bool]] = []
    for pair in scope.source_pairs:
        if (
            not isinstance(pair, SourcePair)
            or not _valid_path(pair.source_path)
            or not pair.target_paths
            or any(not _valid_path(path) for path in pair.target_paths)
            or not isinstance(pair.map_descendants, bool)
            or pair.source_path in source_paths
        ):
            raise _fail_scope(resource)
        source_paths.add(pair.source_path)
        if len(set(pair.target_paths)) != len(pair.target_paths):
            raise _fail_scope(resource)
        try:
            source = resolve_element_path(root, pair.source_path)
            targets = tuple(resolve_element_path(root, path) for path in pair.target_paths)
        except (ValueError, IndexError):
            raise _fail_scope(resource) from None
        if (
            not _inside(source, body)
            or "tn-source" not in str(source.get("class", "")).split()
            or any(not _inside(target, body) or _inside(target, source) for target in targets)
        ):
            raise _fail_scope(resource)
        pairs.append((source, targets, pair.map_descendants))

    actual_sources = {
        node
        for node in body.iter()
        if isinstance(node.tag, str) and "tn-source" in str(node.get("class", "")).split()
    }
    if actual_sources != {source for source, _, _ in pairs}:
        raise _fail_scope(resource)
    return excluded, pairs


def _bound_assignments(
    root: etree._Element,
    projection: ResourceProjection,
    scope: ResourceThemeScope,
    assignments: tuple[LayoutAssignment, ...],
    *,
    resource: str,
    excluded: Sequence[etree._Element],
    package_protected: bool,
) -> tuple[tuple[RoleAssignment, ...], frozenset[int]]:
    bindings: dict[ElementPath, LayoutBinding] = {}
    target_paths: set[ElementPath] = set()
    for binding in scope.layout_bindings:
        if (
            not isinstance(binding, LayoutBinding)
            or not _valid_path(binding.source_path)
            or not binding.target_paths
            or any(not _valid_path(path) for path in binding.target_paths)
            or len(set(binding.target_paths)) != len(binding.target_paths)
            or binding.source_path in bindings
            or not target_paths.isdisjoint(binding.target_paths)
        ):
            raise _fail_scope(resource)
        bindings[binding.source_path] = binding
        target_paths.update(binding.target_paths)

    by_node = {node: index for index, node in enumerate(projection.nodes)}
    resolved: dict[int, RoleAssignment] = {}
    boundaries: set[int] = set()
    for assignment in assignments:
        binding = bindings.get(assignment.path)
        if binding is None or binding.source_sha256 != assignment.source_sha256:
            raise ThemeError("theme_layout", "invalid_binding", resource=resource)
        try:
            targets = tuple(resolve_element_path(root, path) for path in binding.target_paths)
        except (ValueError, IndexError):
            raise ThemeError("theme_layout", "invalid_binding", resource=resource) from None
        for target in targets:
            index = by_node.get(target)
            if index is None:
                preserved = (
                    package_protected
                    or scope.preserve_resource
                    or any(_inside(target, node) for node in excluded)
                )
                if preserved:
                    continue
                raise ThemeError("theme_layout", "invalid_binding", resource=resource)
            boundaries.add(index)
            if assignment.role is None:
                continue
            candidate = RoleAssignment(index, assignment.role, assignment.level)
            previous = resolved.get(index)
            if previous is not None and previous != candidate:
                raise ThemeError("theme_layout", "invalid_binding", resource=resource)
            resolved[index] = candidate
    return tuple(resolved[index] for index in sorted(resolved)), frozenset(boundaries)


def _path_map(root: etree._Element) -> dict[etree._Element, ElementPath]:
    result: dict[etree._Element, ElementPath] = {}
    stack = [(root, ())]
    while stack:
        node, path = stack.pop()
        result[node] = path
        children = element_children_lxml(node)
        stack.extend(
            (child, (*path, index)) for index, child in reversed(tuple(enumerate(children)))
        )
    return result


def _is_direct(node: etree._Element) -> bool:
    return BILINGUAL_DIRECT_TARGET_CLASS in str(node.get("class", "")).split()


def _role_for(
    node: etree._Element,
    assigned: dict[etree._Element, tuple[str, int | None]],
    owners: set[etree._Element],
) -> tuple[str, int | None] | None:
    current: etree._Element | None = node
    while current is not None:
        if current in owners:
            return assigned.get(current)
        if current is node and not _is_direct(current):
            return None
        current = current.getparent()
    return None


def _topology(
    root: etree._Element, *, flatten_direct: bool
) -> tuple[tuple[tuple[str, str], ...], tuple[etree._Element, ...]]:
    shape: list[tuple[str, str]] = []
    nodes: list[etree._Element] = []

    def visit(node: etree._Element, parent: int) -> None:
        for child in element_children_lxml(node):
            if "tn-source" in str(child.get("class", "")).split():
                continue
            if flatten_direct and _is_direct(child):
                visit(child, parent)
                continue
            index = len(shape)
            shape.append((f"{parent}:{_namespace(child)}", local_name(child.tag)))
            nodes.append(child)
            visit(child, index)

    visit(root, -1)
    return tuple(shape), tuple(nodes)


def _semantic_markers(
    projection: ResourceProjection,
    assignments: tuple[RoleAssignment, ...],
    boundaries: frozenset[int],
    pairs: list[tuple[etree._Element, tuple[etree._Element, ...], bool]],
) -> dict[etree._Element, dict[str, str]]:
    markers: dict[etree._Element, dict[str, str]] = {}
    assigned: dict[etree._Element, tuple[str, int | None]] = {}
    owners = {projection.nodes[index] for index in boundaries}
    for assignment in assignments:
        node = projection.nodes[assignment.node_id]
        assigned[node] = (assignment.role, assignment.level)
        attrs = {"data-tn-role": assignment.role, "data-tn-content": "target"}
        if assignment.level is not None:
            attrs["data-tn-level"] = str(assignment.level)
        markers[node] = attrs

    for source, targets, map_descendants in pairs:
        if source in projection.protected:
            continue
        roles = [_role_for(target, assigned, owners) for target in targets]
        common = (
            roles[0]
            if roles and roles[0] is not None and all(role == roles[0] for role in roles)
            else None
        )
        attrs = {"data-tn-content": "source"}
        if common is not None:
            attrs["data-tn-role"] = common[0]
            if common[1] is not None:
                attrs["data-tn-level"] = str(common[1])
        markers[source] = attrs
        for target in targets:
            if _is_direct(target) and target not in projection.protected:
                direct = markers.setdefault(target, {})
                direct["data-tn-content"] = "target"
                role = _role_for(target, assigned, owners)
                if role is not None:
                    direct["data-tn-role"] = role[0]
                    if role[1] is not None:
                        direct["data-tn-level"] = str(role[1])
        if not map_descendants or len(targets) != 1:
            continue
        source_shape, source_nodes = _topology(source, flatten_direct=False)
        target_shape, target_nodes = _topology(targets[0], flatten_direct=True)
        if source_shape != target_shape:
            continue
        for source_node, target_node in zip(source_nodes, target_nodes, strict=True):
            role = assigned.get(target_node)
            if role is None or source_node in projection.protected:
                continue
            mirrored = markers.setdefault(source_node, {})
            mirrored["data-tn-role"] = role[0]
            if role[1] is not None:
                mirrored["data-tn-level"] = str(role[1])
            mirrored["data-tn-content"] = "source"
    return markers


def _marked_soup(
    root: etree._Element, markers: dict[etree._Element, dict[str, str]], resource: str
) -> tuple[BeautifulSoup, dict[int, etree._Element]]:
    try:
        soup = BeautifulSoup(etree.tostring(root), "xml")
    except (ValueError, TypeError, etree.LxmlError):
        raise ThemeError("theme_css", "invalid_markup", resource=resource) from None
    xml_nodes = [node for node in root.iter() if isinstance(node.tag, str)]
    soup_nodes = [node for node in soup.find_all(True) if isinstance(node, Tag)]
    if len(xml_nodes) != len(soup_nodes) or any(
        local_name(xml.tag).lower() != str(tag.name).split(":")[-1].lower()
        for xml, tag in zip(xml_nodes, soup_nodes, strict=True)
    ):
        raise ThemeError("theme_css", "invalid_markup", resource=resource)
    reverse = {id(tag): xml for xml, tag in zip(xml_nodes, soup_nodes, strict=True)}
    for xml, tag in zip(xml_nodes, soup_nodes, strict=True):
        for name, value in markers.get(xml, {}).items():
            tag[name] = value
    return soup, reverse


def _safe_reset_match(node: etree._Element, rule: CssRule) -> bool:
    if not any(declaration_properties(item.name) & _INHERITED_RESETS for item in rule.declarations):
        return True
    return all(
        local_name(ancestor.tag).lower() not in _RESET_BARRIERS
        for ancestor in (node, *node.iterancestors())
    )


def _match_rules(
    soup: BeautifulSoup,
    reverse: dict[int, etree._Element],
    rules: Sequence[CssRule],
    projection: ResourceProjection,
    *,
    source_only: bool,
    resource: str,
) -> tuple[list[tuple[CssRule, tuple[etree._Element, ...]]], list[tuple[str, str]]]:
    matched: list[tuple[CssRule, tuple[etree._Element, ...]]] = []
    warnings: list[tuple[str, str]] = []
    for rule in rules:
        selected: list[etree._Element] = []
        for tag in soup.select(rule.selector):
            node = reverse.get(id(tag))
            note_descendant = any(
                "data-tn-note-kind" in ancestor.attrs
                for ancestor in (tag, *tag.parents)
                if isinstance(ancestor, Tag)
            )
            if (
                not source_only
                and "data-tn-role" not in tag.attrs
                and "data-tn-note-kind" not in tag.attrs
                and not note_descendant
            ):
                continue
            if (
                node is None
                or node in projection.protected
                or (
                    not _safe_reset_match(node, rule)
                    and not ("[data-tn-note-kind" in rule.selector and note_descendant)
                )
            ):
                continue
            in_source = node in projection.source_nodes
            if in_source != source_only:
                continue
            selected.append(node)
        unique = tuple(dict.fromkeys(selected))
        if not unique:
            warnings.append(("selector_no_match", resource))
        matched.append((rule, unique))
    return matched, warnings


def _css_rule(rule: CssRule, selectors: str) -> str:
    declarations = "".join(f"{item.name}:{item.value} !important;" for item in rule.declarations)
    body = f"{selectors}{{{declarations}}}"
    if rule.media is None:
        return body
    return f"@media (prefers-color-scheme: {rule.media}){{{body}}}"


def _compile_css(
    matches: list[tuple[CssRule, tuple[etree._Element, ...]]],
    addresses: dict[etree._Element, str],
    guard_count: int,
) -> bytes:
    guard = ":not(#tn-theme-never)" * guard_count
    output: list[str] = []
    for rule, nodes in matches:
        if not nodes:
            continue
        selectors = ",".join(f'[data-tn-theme-node="{addresses[node]}"]{guard}' for node in nodes)
        output.append(_css_rule(rule, selectors))
    return "\n".join(output).encode("utf-8")


def _theme_link(
    root: etree._Element,
    paths: dict[etree._Element, ElementPath],
    resource: str,
    css_path: str,
) -> tuple[ElementPath, tuple[tuple[str, str], ...]]:
    head_nodes = [node for node in root.iter() if local_name(node.tag).lower() == "head"]
    if len(head_nodes) != 1:
        raise ThemeError("theme_css", "missing_head", resource=resource)
    relative = quote(
        posixpath.relpath(css_path, posixpath.dirname(resource) or "."),
        safe="/",
    )
    return paths[head_nodes[0]], (
        ("rel", "stylesheet"),
        ("type", "text/css"),
        ("href", relative),
    )


def _inline_changes(
    order: list[etree._Element],
    rules_by_node: dict[etree._Element, list[CssRule]],
    paths: dict[etree._Element, ElementPath],
    indices: dict[etree._Element, int],
    *,
    resource: str,
) -> tuple[InlineChange, ...]:
    changes: list[InlineChange] = []
    for node in order:
        rules = rules_by_node.get(node)
        before = node.get("style")
        if rules is None or before is None:
            continue
        after, declarations = normalize_inline(
            before, rules, resource=resource, node_id=indices[node]
        )
        if after != before:
            changes.append(InlineChange(paths[node], before, after, declarations))
    return tuple(changes)


def _parse_theme_resource(data: bytes, resource: str) -> tuple[etree._ElementTree, str]:
    try:
        return parse_source_markup(data)
    except (ValueError, etree.LxmlError):
        raise ThemeError("theme_css", "invalid_markup", resource=resource) from None


def plan_resource(
    archive: zipfile.ZipFile,
    data: bytes,
    resource_href: str,
    scope: ResourceThemeScope,
    bundle: ThemeBundle,
    general_rules: Sequence[CssRule],
    bilingual_rules: Sequence[CssRule],
    *,
    ordinal: int,
    css_path: str,
    package_protected: bool = False,
    layout_assignments: tuple[LayoutAssignment, ...] = (),
    note_relations: NoteRelations | None = None,
) -> tuple[ResourceThemePlan | None, tuple[tuple[str, str], ...], Counter[str], int]:
    tree, mode = _parse_theme_resource(data, resource_href)
    root = tree.getroot()
    excluded, pairs = _resolve_scope(root, scope, resource_href)
    initial = build_projection(
        root, excluded=excluded, skip_resource=package_protected or scope.preserve_resource
    )
    for source, targets, _ in pairs:
        if any(target in initial.protected for target in targets):
            excluded.append(source)
    projection = build_projection(
        root,
        excluded=excluded,
        skip_resource=package_protected or scope.preserve_resource,
    )
    body = _body(root, resource_href)
    protected_count = sum(1 for node in body.iter() if node in projection.protected)
    assignments, boundaries = _bound_assignments(
        root,
        projection,
        scope,
        layout_assignments,
        resource=resource_href,
        excluded=excluded,
        package_protected=package_protected,
    )
    if package_protected or scope.preserve_resource:
        return None, (), Counter(), protected_count
    markers = _semantic_markers(projection, assignments, boundaries, pairs)
    note_changes = plan_note_markers(
        root,
        resource_href,
        scope,
        note_relations if bundle.note_markers else None,
        markers,
    )
    soup, reverse = _marked_soup(root, markers, resource_href)
    general_matches, warnings = _match_rules(
        soup, reverse, general_rules, projection, source_only=False, resource=resource_href
    )
    bilingual_matches, bilingual_warnings = _match_rules(
        soup, reverse, bilingual_rules, projection, source_only=True, resource=resource_href
    )
    warnings.extend(bilingual_warnings)
    matches = [*general_matches, *bilingual_matches]
    matched_nodes = {node for _, nodes in matches for node in nodes}
    if not markers and not matched_nodes and not note_changes:
        return None, tuple(warnings), Counter(), protected_count

    order = [node for node in root.iter() if isinstance(node.tag, str)]
    indices = {node: index for index, node in enumerate(order)}
    addresses = {node: f"n{indices[node]}" for node in matched_nodes}
    for node, address in addresses.items():
        markers.setdefault(node, {})["data-tn-theme-node"] = address

    rules_by_node: dict[etree._Element, list[CssRule]] = {}
    for rule, nodes in matches:
        for node in nodes:
            rules_by_node.setdefault(node, []).append(rule)
    paths = _path_map(root)
    inline_changes = _inline_changes(
        order,
        rules_by_node,
        paths,
        indices,
        resource=resource_href,
    )
    stylesheets = collect_source_stylesheets(archive, root, resource_href)
    guards = source_specificity_bound(stylesheets, resource=resource_href)
    css = _compile_css(matches, addresses, guards)
    marker_changes = tuple(
        MarkerChange(
            paths[node],
            tuple((name, attrs[name]) for name in _RESERVED_ATTRIBUTES if name in attrs),
        )
        for node in order
        if (attrs := markers.get(node))
    )
    roles = Counter(attrs["data-tn-role"] for attrs in markers.values() if "data-tn-role" in attrs)
    head_path, link_attributes = _theme_link(root, paths, resource_href, css_path)
    plan = ResourceThemePlan(
        resource_href=resource_href,
        before_sha256=hashlib.sha256(data).hexdigest(),
        before_tree_sha256=tree_sha256(tree, resource=resource_href),
        parse_mode=mode,
        scope=scope,
        markers=marker_changes,
        inline_changes=inline_changes,
        css_path=css_path,
        css_id=f"tn-theme-style-{ordinal}",
        css=css,
        head_path=head_path,
        link_attributes=link_attributes,
        note_changes=note_changes,
    )
    validate_resource_plan(
        tree,
        plan,
        note_relations=note_relations if bundle.note_markers else None,
    )
    return plan, tuple(warnings), roles, protected_count


def _inline_signature(value: str, resource: str) -> list[tuple[str, str, str, bool]]:
    try:
        items = tinycss2.parse_declaration_list(
            value,
            skip_whitespace=False,
            skip_comments=False,
        )
    except RecursionError:
        raise ThemeError("theme_verify", "invalid_plan", resource=resource) from None
    signature: list[tuple[str, str, str, bool]] = []
    for item in items:
        if item.type == "declaration":
            signature.append(
                (
                    item.type,
                    item.lower_name,
                    tinycss2.serialize(item.value),
                    bool(item.important),
                )
            )
        elif item.type in {"whitespace", "comment"}:
            signature.append((item.type, "", tinycss2.serialize([item]), False))
        else:
            raise ThemeError("theme_verify", "invalid_plan", resource=resource)
    return signature


def _plan_projection(
    root: etree._Element,
    plan: ResourceThemePlan,
    *,
    package_protected: bool,
) -> tuple[ResourceProjection, list[tuple[etree._Element, tuple[etree._Element, ...], bool]]]:
    try:
        excluded, pairs = _resolve_scope(root, plan.scope, plan.resource_href)
        initial = build_projection(
            root,
            excluded=excluded,
            skip_resource=package_protected or plan.scope.preserve_resource,
        )
        for source, targets, _ in pairs:
            if any(target in initial.protected for target in targets):
                excluded.append(source)
        projection = build_projection(
            root,
            excluded=excluded,
            skip_resource=package_protected or plan.scope.preserve_resource,
        )
    except ThemeError:
        raise ThemeError("theme_verify", "invalid_plan", resource=plan.resource_href) from None
    return projection, pairs


def validate_resource_plan(
    tree: etree._ElementTree,
    plan: ResourceThemePlan,
    *,
    package_protected: bool = False,
    note_relations: NoteRelations | None = None,
) -> None:
    """在应用前仅根据冻结计划重建保护边界并核验操作账本。"""

    def invalid() -> ThemeError:
        return ThemeError("theme_verify", "invalid_plan", resource=plan.resource_href)

    root = tree.getroot()
    projection, pairs = _plan_projection(
        root,
        plan,
        package_protected=package_protected,
    )
    expected_notes = (
        plan_note_changes(root, plan.resource_href, plan.scope, note_relations)
        if note_relations is not None
        else ()
    )
    if expected_notes != plan.note_changes:
        raise invalid()
    marker_paths = [change.path for change in plan.markers]
    inline_paths = [change.path for change in plan.inline_changes]
    if len(set(marker_paths)) != len(marker_paths) or len(set(inline_paths)) != len(inline_paths):
        raise invalid()
    try:
        marker_nodes = [
            (resolve_element_path(root, change.path), change) for change in plan.markers
        ]
        inline_nodes = [
            (resolve_element_path(root, change.path), change) for change in plan.inline_changes
        ]
        head = resolve_element_path(root, plan.head_path)
    except (ValueError, IndexError):
        raise invalid() from None
    if local_name(head.tag).lower() != "head":
        raise invalid()

    authorized_sources = {source for source, _, _ in pairs}
    order_indices = {
        node: index
        for index, node in enumerate(node for node in root.iter() if isinstance(node.tag, str))
    }
    attrs_by_node = {node: dict(change.attributes) for node, change in marker_nodes}
    address_values: set[str] = set()
    for node, change in marker_nodes:
        if node in projection.protected or not change.attributes:
            raise invalid()
        names = [name for name, _ in change.attributes]
        attrs = dict(change.attributes)
        if (
            len(set(names)) != len(names)
            or any(name not in _RESERVED_ATTRIBUTES for name in names)
            or tuple(names) != tuple(name for name in _RESERVED_ATTRIBUTES if name in attrs)
        ):
            raise invalid()
        if any(node.get(name) is not None for name in names):
            raise invalid()
        role = attrs.get("data-tn-role")
        level = attrs.get("data-tn-level")
        content = attrs.get("data-tn-content")
        address = attrs.get("data-tn-theme-node")
        if role is not None and re.fullmatch(r"[a-z][a-z0-9-]{0,31}", role) is None:
            raise invalid()
        if level is not None and (role != "heading" or level not in {str(i) for i in range(1, 7)}):
            raise invalid()
        if content not in {None, "source", "target"}:
            raise invalid()
        in_source = any(_inside(node, source) for source in authorized_sources)
        if content == "source" and not in_source:
            raise invalid()
        if content == "target" and in_source:
            raise invalid()
        if address is not None:
            if address != f"n{order_indices[node]}" or address in address_values:
                raise invalid()
            address_values.add(address)

    if any(
        source not in projection.protected
        and attrs_by_node.get(source, {}).get("data-tn-content") != "source"
        for source in authorized_sources
    ):
        raise invalid()

    for node, change in inline_nodes:
        if (
            node in projection.protected
            or node.get("style") != change.before
            or "data-tn-theme-node" not in attrs_by_node.get(node, {})
        ):
            raise invalid()
        before = _inline_signature(change.before, plan.resource_href)
        after = _inline_signature(change.after, plan.resource_href)
        if len(before) != len(after):
            raise invalid()
        changed: list[str] = []
        for left, right in zip(before, after, strict=True):
            if left[:3] != right[:3]:
                raise invalid()
            if left[3] == right[3]:
                continue
            if left[0] != "declaration" or not left[3] or right[3]:
                raise invalid()
            changed.append(left[1])
        if tuple(changed) != change.declarations:
            raise invalid()

    try:
        plan.css.decode("utf-8")
    except UnicodeDecodeError:
        raise invalid() from None
