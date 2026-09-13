"""EPUB container, package, and manifest resource validation stages."""

from __future__ import annotations

import zipfile
from collections import Counter
from typing import Any

from bs4 import BeautifulSoup

from trans_novel.assemble.epub.verification import archive_compare as compare
from trans_novel.assemble.epub.verification import archive_model, structure
from trans_novel.epub.package import HTML_MEDIA, NCX_MEDIA
from trans_novel.epub.package import read_package as read_package_model


def read_package(
    zf: Any,
    archive: set[str],
    failures: list[dict[str, str]],
    checked: dict[str, int],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    model_info = read_package_model(zf, failures)
    checked["resources"] += 1
    checked["parse"] += 1
    if not model_info["valid"]:
        return None, None
    return model_info, model_info["model"]


def check_manifest_resources(
    model: dict[str, Any],
    opf_path: str,
    archive: set[str],
    failures: list[dict[str, str]],
    checked: dict[str, int],
) -> None:
    items = model["items"]
    manifest_ids = [entry["id"] for entry in items]
    for entry in items:
        if not entry["id"]:
            failures.append(
                archive_model.item("resources", "manifest_id_missing", opf_path, "manifest")
            )
        if not entry["href"]:
            failures.append(
                archive_model.item(
                    "resources", "manifest_href_missing", opf_path, entry["id"] or "manifest"
                )
            )
        if not entry["media"]:
            failures.append(
                archive_model.item(
                    "resources", "manifest_media_missing", opf_path, entry["id"] or "manifest"
                )
            )
    for item_id, count in sorted(Counter(manifest_ids).items()):
        if item_id and count > 1:
            failures.append(
                archive_model.item("resources", "manifest_id_duplicate", opf_path, item_id)
            )
    for item_index, entry in enumerate(items):
        checked["resources"] += 1
        if not entry["id"] or any(previous["id"] == entry["id"] for previous in items[:item_index]):
            continue
        target = entry["path"]
        if target is None or target not in archive:
            failures.append(
                archive_model.item("resources", "missing_manifest_resource", opf_path, entry["id"])
            )
    resolved = model["resolved"]
    manifest_paths = {entry["path"] for entry in resolved}
    special = {"mimetype", "META-INF/container.xml", opf_path}
    for name in sorted(archive - manifest_paths - special):
        if name.endswith("/"):
            continue
        if not name.startswith("META-INF/"):
            failures.append(
                archive_model.item(
                    "resources",
                    "unmanifested_resource",
                    name,
                    "archive",
                )
            )
    for entry in resolved:
        properties = entry["properties"].split()
        if "nav" in properties and entry["media"] != "application/xhtml+xml":
            failures.append(
                archive_model.item(
                    "nav", "nav_manifest_media", opf_path, entry["id"] or entry["href"]
                )
            )
        if model["spine_toc"] and entry["id"] == model["spine_toc"] and entry["media"] != NCX_MEDIA:
            failures.append(
                archive_model.item(
                    "nav", "ncx_manifest_media", opf_path, entry["id"] or entry["href"]
                )
            )
    if model["spine_toc"] and not any(
        entry["id"] == model["spine_toc"] and entry["media"] == NCX_MEDIA for entry in resolved
    ):
        failures.append(archive_model.item("nav", "spine_toc_unresolved", opf_path, "toc"))
    spine_paths: list[str] = []
    for item_id in model["spine_ids"]:
        checked["spine"] += 1
        matching = [item for item in resolved if item["id"] == item_id]
        if len(matching) != 1:
            failures.append(archive_model.item("spine", "unresolved_idref", opf_path, item_id))
        elif matching[0]["media"] not in HTML_MEDIA:
            failures.append(archive_model.item("spine", "non_content_item", opf_path, item_id))
        else:
            spine_paths.append(matching[0]["path"])
    if not spine_paths:
        failures.append(
            archive_model.item("spine", "empty_spine", opf_path, "spine has no content")
        )


def compare_source_archive(
    source_path: Any,
    model_info: dict[str, Any],
    model: dict[str, Any],
    current_assets: dict[str, Any],
    current_graph: Counter,
    soups: dict[str, Any],
    bilingual: bool | None,
    checked: dict[str, int],
    failures: list[dict[str, str]],
) -> None:
    if not source_path.is_file():
        failures.append(
            archive_model.item("assets", "source_missing", "<source>", "source_unreadable")
        )
        return
    try:
        with zipfile.ZipFile(source_path, "r") as source_zip:
            source_info = read_package_model(source_zip, [])
            source_model = source_info["model"]
            source_asset_failures: list[dict[str, str]] = []
            source_assets = archive_model.resource_hashes(
                source_zip,
                source_info,
                source_info["opf_path"],
                source_asset_failures,
                checked,
            )
            failures.extend(source_asset_failures)
            compare.compare_source_models(
                source_info,
                model_info | {"model": model},
                source_assets,
                current_assets,
                failures,
            )
            source_soups: dict[str, BeautifulSoup] = {}
            source_ids: dict[str, set[str]] = {}
            source_failures: list[dict[str, str]] = []
            for source_item in source_model["resolved"]:
                if (
                    source_item["media"] not in HTML_MEDIA
                    and source_item["media"] != NCX_MEDIA
                    and "nav" not in source_item["properties"].split()
                ):
                    continue
                source_data = archive_model.model_read(
                    source_zip, source_item["path"], source_failures
                )
                if source_data is None:
                    continue
                source_soup, _ = structure.html_soup(source_data, source_item["media"])
                source_soups[source_item["path"]] = source_soup
                source_ids[source_item["path"]] = structure.ids(source_soup)
            temp_checked = dict.fromkeys(checked, 0)
            source_graph = structure.graph_from_soups(
                source_soups,
                source_info["archive"],
                source_ids,
                [],
                [],
                temp_checked,
            )
            failures.extend(source_failures)
            for key in set(source_graph) | set(current_graph):
                if current_graph[key] != source_graph[key]:
                    failures.append(
                        archive_model.item(
                            "internal_links", "reference_graph_mismatch", key[0], "source"
                        )
                    )
            source_identifier_set = {
                (resource, identifier)
                for resource, soup in source_soups.items()
                for identifier in structure.ids(soup)
            }
            current_identifier_set = {
                (resource, identifier)
                for resource, soup in soups.items()
                for identifier in structure.ids(soup)
            }
            for resource, _identifier in sorted(source_identifier_set - current_identifier_set):
                failures.append(
                    archive_model.item("anchors", "anchor_graph_mismatch", resource, "source")
                )
            source_inline = structure.inline_hashes(source_soups)
            output_inline = structure.inline_hashes(soups)
            for resource in sorted(set(source_inline) | set(output_inline)):
                expected_inline = source_inline.get(resource, [])
                actual_inline = output_inline.get(resource, [])
                if actual_inline != expected_inline:
                    failures.append(
                        archive_model.item(
                            "resources", "inline_resource_mismatch", resource, "style_or_script"
                        )
                    )
    except (OSError, zipfile.BadZipFile):
        failures.append(
            archive_model.item("assets", "source_unreadable", "<source>", "source_unreadable")
        )
