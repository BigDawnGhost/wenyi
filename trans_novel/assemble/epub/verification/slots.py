"""Exact schema4 slot and DOM comparison proofs."""

from __future__ import annotations

import hashlib
import zipfile
from collections import defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from lxml import etree

from trans_novel.assemble.epub.rendering import dedupe_segment_mappings, segment_needs_source
from trans_novel.assemble.epub.rendering.theme import NotePathMapping
from trans_novel.assemble.epub.verification import archive_model, dom, preservation
from trans_novel.assemble.epub.verification import bilingual as bilingual_module
from trans_novel.assemble.epub.verification import navigation as nav_module
from trans_novel.epub.markup import resource_parser
from trans_novel.epub.package import HTML_MEDIA, NCX_MEDIA, read_package
from trans_novel.epub.slots import normalized_source_text, slot_contract_digest
from trans_novel.ingest.models import preserved_toc_entry_ids

MAX_MEMBER_BYTES = archive_model.MAX_MEMBER_BYTES


def compare_dom(
    source: etree._Element,
    output: etree._Element,
    slots: dict[tuple[tuple[int, ...], str], Any],
    path: tuple[int, ...] = (),
    *,
    toc_label_paths: set[tuple[int, ...]] | None = None,
    allow_root_language: bool = True,
    language_paths: set[tuple[int, ...]] | None = None,
) -> bool:
    if source.tag != output.tag:
        return False
    allow_language = (allow_root_language and path == ()) or (
        language_paths is not None and path in language_paths
    )
    source_attrs = {
        key: value
        for key, value in source.attrib.items()
        if not (allow_language and bilingual_module.lang_attr(key))
    }
    output_attrs = {
        key: value
        for key, value in output.attrib.items()
        if not (allow_language and bilingual_module.lang_attr(key))
    }
    if source_attrs != output_attrs:
        return False
    if (path, "text") not in slots and source.text != output.text:
        return False
    source_children = list(source)
    output_children = list(output)
    if len(source_children) != len(output_children):
        return False
    allow_toc_tail = bool(
        toc_label_paths
        and any(path[: len(label_path)] == label_path for label_path in toc_label_paths)
    )
    element_index = 0
    for source_child, output_child in zip(source_children, output_children, strict=True):
        if not isinstance(source_child.tag, str) or not isinstance(output_child.tag, str):
            if source_child.tag != output_child.tag or source_child.text != output_child.text:
                return False
            if (not allow_toc_tail and source_child.tail != output_child.tail) or (
                allow_toc_tail and output_child.tail is not None
            ):
                return False
            continue
        child_path = (*path, element_index)
        element_index += 1
        if not compare_dom(
            source_child,
            output_child,
            slots,
            child_path,
            toc_label_paths=toc_label_paths,
            allow_root_language=allow_root_language,
            language_paths=language_paths,
        ):
            return False
        if (child_path, "tail") not in slots and source_child.tail != output_child.tail:
            return False
    return True


def _resource_data(source_zip, output_zip, resource, resources, failures):
    source_names = set(source_zip.namelist())
    output_names = set(output_zip.namelist())
    if resource not in source_names or resource not in output_names:
        failures.append(
            archive_model.item("state", "resource_missing", resource, "schema4_locator")
        )
        return None
    try:
        source_data = archive_model.read_member(source_zip, source_zip.getinfo(resource))
        output_data = archive_model.read_member(output_zip, output_zip.getinfo(resource))
    except (KeyError, archive_model.MemberError):
        failures.append(archive_model.item("zip", "member_read", resource, "member"))
        return None
    expected = resources.get(resource, {})
    if expected.get("resource_sha256") and hashlib.sha256(source_data).hexdigest() != str(
        expected["resource_sha256"]
    ):
        failures.append(archive_model.item("assets", "source_digest_mismatch", resource, "state"))
        return None
    return source_data, output_data, expected


def _parse_resource(resource, source_data, output_data, expected, failures, warnings, checked):
    try:
        source_tree, source_mode, source_diag = resource_parser(source_data)
        output_tree, output_mode, output_diag = resource_parser(output_data)
    except Exception:
        failures.append(archive_model.item("parse", "resource_unreadable", resource, "xhtml"))
        return None
    checked["parse"] = checked.get("parse", 0) + 2
    expected_mode = str(expected.get("parse_mode", ""))
    if expected_mode and source_mode != expected_mode:
        failures.append(archive_model.item("parse", "parse_mode_mismatch", resource, "state"))
    if source_mode == "recovered":
        warnings.append(archive_model.item("parse", "recovered_resource", resource, "recovered"))
        persisted_codes = dom.diagnostic_codes(expected.get("parser_diagnostics"))
        actual_codes = dom.diagnostic_codes(source_diag)
        if not isinstance(expected.get("parser_diagnostics"), list):
            failures.append(
                archive_model.item("parse", "recovered_diagnostic_missing", resource, "state")
            )
        elif persisted_codes != actual_codes:
            failures.append(
                archive_model.item("parse", "recovered_diagnostic_mismatch", resource, "state")
            )
        else:
            for domain, kind in actual_codes:
                code = f"recovered_diagnostic_{domain or 'unknown'}_{kind or 'unknown'}"
                warnings.append(archive_model.item("parse", code, resource, "recovered"))
        if output_mode == "recovered" and dom.diagnostic_codes(output_diag) != persisted_codes:
            failures.append(
                archive_model.item(
                    "parse", "recovered_output_diagnostic_mismatch", resource, "state"
                )
            )
    if output_mode == "recovered" and expected_mode == "xml":
        failures.append(
            archive_model.item("parse", "unexpected_recovery", resource, "strict_required")
        )
    return source_tree, output_tree, source_mode, output_mode


def _check_segments(root_source, resource, segments, bilingual, slot_map, direct_cleared, failures):
    for segment in segments:
        state = segment.epub_state
        assert state is not None
        block_source = dom.resolve_path_lxml(root_source, tuple(state.block_path))
        if block_source is None:
            failures.append(archive_model.item("dom", "block_locator_missing", resource, "state"))
            continue
        if (
            bilingual
            and segment_needs_source(segment)
            and any(
                isinstance(child.tag, str) and archive_model.local_name(child.tag).lower() == "br"
                for child in block_source
            )
        ):
            direct_cleared.update(
                (tuple(state.block_path) + tuple(slot.element_path), slot.field)
                for slot in state.slots
            )
        fingerprint = hashlib.sha256(
            etree.tostring(block_source, encoding="utf-8", with_tail=False)
        ).hexdigest()
        if fingerprint != state.block_fingerprint:
            failures.append(
                archive_model.item("dom", "block_fingerprint_mismatch", resource, "state")
            )
        if segment.source != normalized_source_text(state.slots):
            failures.append(
                archive_model.item("state", "source_derivation_mismatch", resource, "state")
            )
        if state.slot_contract_sha256 != slot_contract_digest(state.slots):
            failures.append(
                archive_model.item("state", "slot_contract_mismatch", resource, "state")
            )
        seen: set[tuple[tuple[int, ...], str]] = set()
        for slot in state.slots:
            location = (tuple(state.block_path) + tuple(slot.element_path), slot.field)
            if location in seen:
                failures.append(archive_model.item("state", "slot_overlap", resource, "state"))
            seen.add(location)
            slot_map[location] = (slot, segment.preserve_source)
            owner = dom.resolve_path_lxml(root_source, location[0])
            if owner is None:
                failures.append(
                    archive_model.item("dom", "slot_locator_missing", resource, "state")
                )
            elif getattr(owner, slot.field) != slot.source_value:
                failures.append(
                    archive_model.item("dom", "source_slot_mismatch", resource, "state")
                )


def _check_output_slots(root_output, resource, slot_map, direct_cleared, failures, differences):
    for (location, field), allowed in slot_map.items():
        owner = dom.resolve_path_lxml(root_output, location)
        if owner is None:
            failures.append(archive_model.item("dom", "slot_locator_missing", resource, "output"))
            continue
        if isinstance(allowed, dict) and allowed.get("kind") == "toc":
            actual_value = getattr(owner, field)
            if actual_value != allowed.get("expected"):
                failures.append(
                    archive_model.item("nav", "label_value_mismatch", resource, "target")
                )
            if allowed.get("count") and actual_value != allowed.get("source"):
                differences["toc_labels"] += 1
            continue
        slot, preserve_source = allowed
        expected_value = (
            slot.source_value
            if preserve_source
            else slot.target_value
            if slot.target_value is not None
            else slot.source_value
        )
        actual_value = getattr(owner, field)
        cleared = actual_value is None and (location, field) in direct_cleared
        empty_serialized = actual_value is None and expected_value == ""
        if actual_value != expected_value and not cleared and not empty_serialized:
            failures.append(archive_model.item("dom", "slot_value_mismatch", resource, "target"))
        if cleared or actual_value != slot.source_value:
            differences["text_slots"] += 1


def _validate_resource(
    source_zip,
    output_zip,
    resource,
    segments,
    resources,
    toc_entries,
    bilingual,
    source_lang,
    target_lang,
    bilingual_order,
    failures,
    warnings,
    checked,
    differences,
    note_mappings: tuple[NotePathMapping, ...],
):
    data = _resource_data(source_zip, output_zip, resource, resources, failures)
    if data is None:
        return
    parsed = _parse_resource(resource, *data, failures, warnings, checked)
    if parsed is None:
        return
    source_tree, output_tree, _, _ = parsed
    root_source, root_output = source_tree.getroot(), output_tree.getroot()
    mapped_nodes: list[tuple[NotePathMapping, etree._Element]] = []
    for mapping in note_mappings:
        mapped = dom.resolve_path_lxml(root_output, mapping.target_path)
        if mapped is None:
            failures.append(archive_model.item("resources", "theme_verify", resource, "invalid"))
        else:
            mapped_nodes.append((mapping, mapped))
    is_ncx_resource = any(
        isinstance(node.tag, str) and archive_model.local_name(node.tag).lower() == "navmap"
        for node in root_source.iter()
    )
    preservation.check_root_language(
        root_source,
        root_output,
        resource,
        target_lang,
        is_ncx_resource,
        segments,
        failures,
        differences,
    )
    slot_map: dict[tuple[tuple[int, ...], str], Any] = {}
    toc_label_paths: set[tuple[int, ...]] = set()
    direct_cleared: set[tuple[tuple[int, ...], str]] = set()
    _check_segments(root_source, resource, segments, bilingual, slot_map, direct_cleared, failures)
    if bilingual and any(
        isinstance(node.tag, str) and archive_model.local_name(node.tag).lower() in {"html", "body"}
        for node in root_source.iter()
    ):
        differences["bilingual_nodes"] += bilingual_module.bilingual_proof(
            root_source,
            root_output,
            segments,
            source_lang=source_lang,
            order=bilingual_order,
            resource=resource,
            failures=failures,
        )
        for mapping, mapped in mapped_nodes:
            if dom.element_path_lxml(root_output, mapped) != mapping.source_path:
                failures.append(
                    archive_model.item("resources", "theme_verify", resource, "invalid")
                )
    elif note_mappings:
        failures.append(archive_model.item("resources", "theme_verify", resource, "invalid"))
    language_paths = preservation.preserved_language_paths(
        root_source, root_output, resource, segments, source_lang, failures
    )
    is_navigation = any(
        isinstance(node.tag, str)
        and archive_model.local_name(node.tag).lower() in {"nav", "navmap"}
        for node in root_source.iter()
    )
    if is_navigation:
        nav_module.navigation_slots(
            root_source,
            root_output,
            resource,
            toc_entries,
            slot_map,
            toc_label_paths,
            language_paths,
            source_lang,
            failures,
        )
    if not compare_dom(
        root_source,
        root_output,
        slot_map,
        toc_label_paths=toc_label_paths,
        allow_root_language=not is_ncx_resource,
        language_paths=language_paths,
    ):
        failures.append(archive_model.item("dom", "unauthorized_dom_change", resource, "immutable"))
    _check_output_slots(root_output, resource, slot_map, direct_cleared, failures, differences)


def _validate_resources(
    source_zip: zipfile.ZipFile,
    output_zip: zipfile.ZipFile,
    by_resource: dict[str, list[Any]],
    resources: dict[str, Any],
    toc_entries: list[dict[str, Any]],
    bilingual: bool,
    source_lang: str,
    target_lang: str | None,
    bilingual_order: str,
    failures: list[dict[str, str]],
    warnings: list[dict[str, str]],
    checked: dict[str, int],
    differences: dict[str, int],
    note_mappings: Mapping[str, tuple[NotePathMapping, ...]],
) -> None:
    with source_zip, output_zip:
        for resource, segments in sorted(by_resource.items()):
            _validate_resource(
                source_zip,
                output_zip,
                resource,
                segments,
                resources,
                toc_entries,
                bilingual,
                source_lang,
                target_lang,
                bilingual_order,
                failures,
                warnings,
                checked,
                differences,
                note_mappings.get(resource, ()),
            )


def slot_proof(
    source_path: Path,
    output_path: Path,
    store: Any,
    resources: dict[str, Any],
    chapters: list[Any],
    *,
    bilingual: bool,
    target_lang: str | None = None,
    bilingual_order: str = "target_first",
    failures: list[dict[str, str]],
    warnings: list[dict[str, str]],
    checked: dict[str, int],
    note_mappings: Mapping[str, tuple[NotePathMapping, ...]] | None = None,
) -> dict[str, int]:
    all_segments = [
        segment
        for chapter in chapters
        for segment in chapter.segments
        if segment.epub_state is not None and segment.epub_state.resource_href
    ]
    try:
        deduped_segments = dedupe_segment_mappings(all_segments)
    except ValueError:
        failures.append(archive_model.item("state", "slot_mapping_ambiguous", "<state>", "schema4"))
        deduped_segments = all_segments
    by_resource: dict[str, list[Any]] = defaultdict(list)
    for segment in deduped_segments:
        state = segment.epub_state
        assert state is not None
        by_resource[state.resource_href].append(segment)
    note_mappings = note_mappings or {}
    differences = {"text_slots": 0, "toc_labels": 0, "language_fields": 0, "bilingual_nodes": 0}
    try:
        source_zip = zipfile.ZipFile(source_path, "r")
        output_zip = zipfile.ZipFile(output_path, "r")
    except (OSError, zipfile.BadZipFile):
        failures.append(
            archive_model.item("state", "source_reopen_failed", "<source>", "unreadable")
        )
        return differences
    try:
        archive_info = read_package(source_zip, [])
        xml_resources = {
            item["path"]
            for item in archive_info.get("model", {}).get("resolved", [])
            if item.get("media") in HTML_MEDIA
            or item.get("media") == NCX_MEDIA
            or "nav" in item.get("properties", "").split()
        }
        for resource in sorted(path for path in xml_resources if path):
            by_resource.setdefault(resource, [])
    except Exception:
        pass
    try:
        manifest = store.load_manifest()
    except Exception:
        manifest = {}
    try:
        source_lang = str(manifest.get("source_lang", ""))
    except AttributeError:
        source_lang = ""
    raw_meta = manifest.get("meta") if isinstance(manifest, dict) else {}
    toc_entries = raw_meta.get("toc_entries") if isinstance(raw_meta, dict) else []
    raw_toc = toc_entries if isinstance(toc_entries, list) else []
    preserved_toc_ids = preserved_toc_entry_ids(chapters, raw_toc)
    toc_entries = [
        {**entry, "preserve_source": True} if entry.get("entry_id") in preserved_toc_ids else entry
        for entry in raw_toc
        if isinstance(entry, dict)
    ]
    root_lang = (
        source_lang
        if chapters and all(chapter.preserve_source for chapter in chapters) and source_lang
        else target_lang
    )
    _validate_resources(
        source_zip,
        output_zip,
        by_resource,
        resources,
        toc_entries,
        bilingual,
        source_lang,
        root_lang,
        bilingual_order,
        failures,
        warnings,
        checked,
        differences,
        note_mappings,
    )

    return differences
