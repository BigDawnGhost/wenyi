"""按预期主题计划独立生成可逆的 EPUB 验证投影。"""

from __future__ import annotations

import hashlib
import os
import posixpath
import tempfile
import zipfile
from collections import Counter
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from copy import copy
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

from lxml import etree

from trans_novel.assemble.epub.rendering.source_dom import (
    element_children_lxml,
    parse_source_markup,
    resolve_element_path,
    serialize_source_tree,
)
from trans_novel.assemble.epub.rendering.theme.contracts import (
    ResourceThemePlan,
    ThemeError,
    ThemePlan,
)
from trans_novel.assemble.epub.rendering.theme.notes import (
    apply_note_changes,
    expected_note_changes,
)
from trans_novel.assemble.epub.rendering.theme.planning import tree_sha256
from trans_novel.epub.archive import (
    MAX_ARCHIVE_BYTES,
    MAX_MEMBER_BYTES,
    MetadataZipFile,
    ZipSafetyError,
    preflight_zip,
    read_member,
)
from trans_novel.epub.notes import NoteRelations
from trans_novel.ingest.epub.reader import read_epub

_RESERVED_ATTRIBUTES = {
    "data-tn-role",
    "data-tn-level",
    "data-tn-content",
    "data-tn-theme-node",
}


def _invalid(resource: str | None = None) -> ThemeError:
    return ThemeError("theme_verify", "invalid_plan", resource=resource)


def _validate_plan_shape(plan: ThemePlan, *, bilingual_note_context: bool) -> None:
    if not isinstance(plan.bilingual, bool):
        raise _invalid()
    opf_dir = posixpath.dirname(plan.opf_path)
    prefix = f"{opf_dir}/tn-theme" if opf_dir else "tn-theme"
    for ordinal, resource in enumerate(plan.resources):
        expected_link = (
            ("rel", "stylesheet"),
            ("type", "text/css"),
            (
                "href",
                quote(
                    posixpath.relpath(
                        resource.css_path,
                        posixpath.dirname(resource.resource_href) or ".",
                    ),
                    safe="/",
                ),
            ),
        )
        if (
            resource.css_path != f"{prefix}/style-{ordinal}.css"
            or resource.css_id != f"tn-theme-style-{ordinal}"
            or resource.link_attributes != expected_link
        ):
            raise _invalid(resource.resource_href)
        if resource.note_changes and plan.source_sha256 is None:
            raise _invalid(resource.resource_href)
        if resource.note_changes:
            if plan.bilingual and not bilingual_note_context:
                raise _invalid(resource.resource_href)
            if not plan.bilingual and any(
                mapping.source_path != mapping.target_path for mapping in resource.scope.note_paths
            ):
                raise _invalid(resource.resource_href)


def theme_summary(plan: ThemePlan) -> dict[str, object]:
    """仅在投影成功后返回由计划推导的紧凑证据。"""
    return {
        "resources": len(plan.resources),
        "role_counts": dict(plan.role_counts),
        "protected_count": plan.protected_count,
        "note_change_count": sum(len(resource.note_changes) for resource in plan.resources),
        "marker_count": sum(len(resource.markers) for resource in plan.resources),
        "normalized_declaration_count": sum(
            len(change.declarations)
            for resource in plan.resources
            for change in resource.inline_changes
        ),
        "warning_counts": dict(sorted(Counter(code for code, _path in plan.warnings).items())),
        "stylesheets": [
            {
                "resource": resource.resource_href,
                "path": resource.css_path,
                "sha256": hashlib.sha256(resource.css).hexdigest(),
            }
            for resource in plan.resources
        ],
    }


def _parse_opf(data: bytes, resource: str) -> etree._ElementTree:
    parser = etree.XMLParser(
        no_network=True,
        recover=False,
        resolve_entities=False,
        remove_comments=False,
        remove_pis=False,
        strip_cdata=False,
    )
    try:
        return etree.fromstring(data, parser).getroottree()
    except (etree.LxmlError, TypeError, ValueError):
        raise _invalid(resource) from None


def _local_name(node: etree._Element) -> str:
    return node.tag.rsplit("}", 1)[-1] if isinstance(node.tag, str) else ""


def _element_paths(root: etree._Element) -> dict[etree._Element, tuple[int, ...]]:
    paths: dict[etree._Element, tuple[int, ...]] = {}
    stack = [(root, ())]
    while stack:
        node, path = stack.pop()
        paths[node] = path
        children = element_children_lxml(node)
        stack.extend(
            (child, (*path, index)) for index, child in reversed(tuple(enumerate(children)))
        )
    return paths


def _reverse_resource(
    data: bytes,
    plan: ResourceThemePlan,
    *,
    source_root: etree._Element | None,
    note_relations: NoteRelations | None,
) -> bytes:
    try:
        tree, _mode = parse_source_markup(data)
        root = tree.getroot()
        if note_relations is not None and source_root is not None:
            apply_note_changes(
                root,
                plan.note_changes,
                reverse=True,
                resource=plan.resource_href,
            )
            if (
                expected_note_changes(
                    source_root,
                    root,
                    plan.resource_href,
                    plan.scope,
                    note_relations,
                )
                != plan.note_changes
            ):
                raise _invalid(plan.resource_href)
        elif plan.note_changes:
            raise _invalid(plan.resource_href)
        paths = _element_paths(root)
        expected_markers = {change.path: dict(change.attributes) for change in plan.markers}
        for change in plan.markers:
            names = [name for name, _value in change.attributes]
            if len(set(names)) != len(names) or set(names) - _RESERVED_ATTRIBUTES:
                raise _invalid(plan.resource_href)
        if len(expected_markers) != len(plan.markers):
            raise _invalid(plan.resource_href)
        for node, path in paths.items():
            actual = {
                name: value for name, value in node.attrib.items() if name in _RESERVED_ATTRIBUTES
            }
            expected = expected_markers.get(path, {})
            if actual != expected or set(expected) - _RESERVED_ATTRIBUTES:
                raise _invalid(plan.resource_href)
        for change in plan.markers:
            node = resolve_element_path(root, change.path)
            for name, _value in change.attributes:
                del node.attrib[name]

        inline_paths = [change.path for change in plan.inline_changes]
        if len(set(inline_paths)) != len(inline_paths):
            raise _invalid(plan.resource_href)
        for change in plan.inline_changes:
            node = resolve_element_path(root, change.path)
            if node.get("style") != change.after:
                raise _invalid(plan.resource_href)
            node.set("style", change.before)

        head = resolve_element_path(root, plan.head_path)
        expected_tag = (
            f"{{{head.tag[1:].split('}', 1)[0]}}}link" if head.tag.startswith("{") else "link"
        )
        expected_attrs = dict(plan.link_attributes)
        matches = [
            node
            for node in root.iter()
            if isinstance(node.tag, str)
            and node.tag == expected_tag
            and dict(node.attrib) == expected_attrs
        ]
        children = element_children_lxml(head)
        if len(matches) != 1 or not children or children[-1] is not matches[0]:
            raise _invalid(plan.resource_href)
        head.remove(matches[0])
        if tree_sha256(tree, resource=plan.resource_href) != plan.before_tree_sha256:
            raise _invalid(plan.resource_href)
        return serialize_source_tree(tree, data, plan.parse_mode)
    except ThemeError:
        raise
    except (AttributeError, IndexError, KeyError, TypeError, ValueError, etree.LxmlError):
        raise _invalid(getattr(plan, "resource_href", None)) from None


def _manifest(root: etree._Element) -> etree._Element | None:
    return next(
        (
            node
            for node in root.iter()
            if isinstance(node.tag, str) and _local_name(node) == "manifest"
        ),
        None,
    )


def _reverse_opf(data: bytes, plan: ThemePlan) -> bytes:
    tree = _parse_opf(data, plan.opf_path)
    manifest = _manifest(tree.getroot())
    if manifest is None:
        raise _invalid(plan.opf_path)
    opf_dir = posixpath.dirname(plan.opf_path)
    children = element_children_lxml(manifest)
    generated = children[-len(plan.resources) :] if plan.resources else []
    if len(generated) != len(plan.resources):
        raise _invalid(plan.opf_path)
    for node, resource in zip(generated, plan.resources, strict=True):
        expected = {
            "id": resource.css_id,
            "href": quote(posixpath.relpath(resource.css_path, opf_dir or "."), safe="/"),
            "media-type": "text/css",
        }
        if _local_name(node) != "item" or dict(node.attrib) != expected:
            raise _invalid(plan.opf_path)
    for node in generated:
        manifest.remove(node)
    if tree_sha256(tree, resource=plan.opf_path) != plan.opf_before_tree_sha256:
        raise _invalid(plan.opf_path)
    return serialize_source_tree(tree, data, "xml")


def _validate_archive(
    archive: zipfile.ZipFile,
    plan: ThemePlan,
    *,
    max_member_bytes: int,
    max_archive_bytes: int,
) -> list[zipfile.ZipInfo]:
    try:
        infos = preflight_zip(
            archive,
            max_member_bytes=max_member_bytes,
            max_archive_bytes=max_archive_bytes,
            read_members=False,
        )
        original_names = [name for name, _digest in plan.members]
        css_names = [resource.css_path for resource in plan.resources]
        expected_names = [*original_names, *css_names]
        actual_names = [info.filename for info in infos]
        if actual_names != expected_names:
            actual_counts = Counter(actual_names)
            expected_counts = Counter(expected_names)
            mismatch = next(
                (
                    name
                    for name in (*actual_names, *expected_names)
                    if actual_counts[name] != expected_counts[name]
                ),
                None,
            )
            raise _invalid(mismatch)
        if len(set(original_names)) != len(original_names) or len(set(css_names)) != len(css_names):
            raise _invalid()
        ledger = dict(plan.members)
        resources = {resource.resource_href: resource for resource in plan.resources}
        if len(resources) != len(plan.resources) or plan.opf_path not in ledger:
            raise _invalid()
        for resource in plan.resources:
            if (
                resource.resource_href not in ledger
                or ledger[resource.resource_href] != resource.before_sha256
                or read_member(
                    archive, archive.getinfo(resource.css_path), max_member_bytes=max_member_bytes
                )
                != resource.css
            ):
                raise _invalid(resource.resource_href)
        return infos
    except ThemeError:
        raise
    except ZipSafetyError as error:
        raise _invalid(error.name or None) from None
    except (AttributeError, KeyError, OSError, TypeError, ValueError, zipfile.BadZipFile):
        raise _invalid() from None


def _write_projection(
    path: str,
    archive: zipfile.ZipFile,
    infos: list[zipfile.ZipInfo],
    plan: ThemePlan,
    max_member_bytes: int,
    *,
    source_roots: Mapping[str, etree._Element],
    note_relations: NoteRelations | None,
) -> None:
    resources = {resource.resource_href: resource for resource in plan.resources}
    ledger = dict(plan.members)
    css_names = {resource.css_path for resource in plan.resources}
    with MetadataZipFile(path, "w") as output:
        output.comment = archive.comment
        for info in infos:
            if info.filename in css_names:
                continue
            data = read_member(archive, info, max_member_bytes=max_member_bytes)
            if info.filename == plan.opf_path:
                data = _reverse_opf(data, plan)
            elif info.filename in resources:
                resource = resources[info.filename]
                data = _reverse_resource(
                    data,
                    resource,
                    source_root=source_roots.get(info.filename),
                    note_relations=note_relations,
                )
            elif hashlib.sha256(data).hexdigest() != ledger[info.filename]:
                raise _invalid(info.filename)
            output.writestr(copy(info), data)


def _remove_temporary(path: str | None) -> None:
    if path is None:
        return
    with suppress(OSError):
        os.unlink(path)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_note_context(
    source_path: Path | None,
    plan: ThemePlan,
    *,
    max_member_bytes: int,
    max_archive_bytes: int,
) -> tuple[dict[str, etree._Element], NoteRelations | None]:
    if plan.source_sha256 is None:
        return {}, None
    if source_path is None:
        raise _invalid()
    digest = _file_sha256(source_path)
    if digest != plan.source_sha256:
        raise _invalid()
    document = read_epub(str(source_path), "", "")
    relations = document.meta.get("epub_notes")
    if not isinstance(relations, dict):
        raise _invalid()
    note_resources = {
        item["resource_href"]
        for item in (*relations["markers"], *relations["targets"])
        if isinstance(item, dict) and isinstance(item.get("resource_href"), str)
    }
    roots: dict[str, etree._Element] = {}
    try:
        with zipfile.ZipFile(source_path) as source:
            preflight_zip(
                source,
                max_member_bytes=max_member_bytes,
                max_archive_bytes=max_archive_bytes,
                read_members=False,
            )
            for resource in plan.resources:
                if resource.resource_href not in note_resources:
                    continue
                data = read_member(
                    source,
                    source.getinfo(resource.resource_href),
                    max_member_bytes=max_member_bytes,
                )
                roots[resource.resource_href] = parse_source_markup(data)[0].getroot()
    except (KeyError, OSError, ValueError, ZipSafetyError, zipfile.BadZipFile, etree.LxmlError):
        raise _invalid() from None
    return roots, cast(NoteRelations, relations)


def _prove_bilingual_note_mappings(
    projected_path: str,
    source_path: Path | None,
    store: Any | None,
    plan: ThemePlan,
    *,
    target_lang: str | None,
    bilingual_order: str,
) -> None:
    mappings = {
        resource.resource_href: resource.scope.note_paths
        for resource in plan.resources
        if resource.note_changes
    }
    if not plan.bilingual or not mappings:
        return
    if source_path is None or store is None:
        raise _invalid()
    try:
        from trans_novel.assemble.epub.verification import slots as slot_module

        manifest = store.load_manifest()
        meta = manifest.get("meta") if isinstance(manifest.get("meta"), dict) else {}
        if meta.get("epub_schema") != 4:
            raise _invalid()
        raw_resources = meta.get("epub_resources", [])
        resources = {
            str(item["href"]): item
            for item in raw_resources
            if isinstance(item, dict) and isinstance(item.get("href"), str)
        }
        chapters = [store.load_chapter(int(item["index"])) for item in manifest["chapters"]]
        failures: list[dict[str, str]] = []
        slot_module.slot_proof(
            source_path,
            Path(projected_path),
            store,
            resources,
            chapters,
            bilingual=True,
            target_lang=target_lang,
            bilingual_order=bilingual_order,
            failures=failures,
            warnings=[],
            checked={},
            note_mappings=mappings,
        )
    except ThemeError:
        raise
    except Exception:
        raise _invalid() from None
    if failures:
        raise _invalid()


@contextmanager
def theme_projection(
    output_path: str | os.PathLike[str],
    plan: ThemePlan | None,
    source_path: str | os.PathLike[str] | None = None,
    *,
    store: Any | None = None,
    target_lang: str | None = None,
    bilingual_order: str = "target_first",
    max_member_bytes: int = MAX_MEMBER_BYTES,
    max_archive_bytes: int = MAX_ARCHIVE_BYTES,
) -> Iterator[Path]:
    """独立核验主题计划；需要时用撤销主题改动的临时 EPUB 副本执行检查。"""
    output = Path(output_path)
    if plan is None:
        yield output
        return
    temporary: str | None = None
    try:
        if not isinstance(plan, ThemePlan):
            raise _invalid()
        _validate_plan_shape(plan, bilingual_note_context=store is not None)
        source_roots, note_relations = _source_note_context(
            Path(source_path) if source_path is not None else None,
            plan,
            max_member_bytes=max_member_bytes,
            max_archive_bytes=max_archive_bytes,
        )
        archive = zipfile.ZipFile(output, "r")
        with archive:
            infos = _validate_archive(
                archive,
                plan,
                max_member_bytes=max_member_bytes,
                max_archive_bytes=max_archive_bytes,
            )
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{output.name}.verify-theme-",
                suffix=".epub",
                dir=output.parent,
            )
            os.close(descriptor)
            _write_projection(
                temporary,
                archive,
                infos,
                plan,
                max_member_bytes,
                source_roots=source_roots,
                note_relations=note_relations,
            )
        assert temporary is not None
        _prove_bilingual_note_mappings(
            temporary,
            Path(source_path) if source_path is not None else None,
            store,
            plan,
            target_lang=target_lang,
            bilingual_order=bilingual_order,
        )
    except ThemeError:
        _remove_temporary(temporary)
        raise
    except (
        AttributeError,
        EOFError,
        IndexError,
        KeyError,
        NotImplementedError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
        ZipSafetyError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        etree.LxmlError,
    ):
        _remove_temporary(temporary)
        raise _invalid() from None
    assert temporary is not None
    try:
        yield Path(temporary)
    finally:
        _remove_temporary(temporary)
