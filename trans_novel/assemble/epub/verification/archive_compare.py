"""Archive model comparison and result finalization stages."""

from __future__ import annotations

from typing import Any

from trans_novel.assemble.epub.verification import archive_model

_CATEGORIES = (
    "zip",
    "resources",
    "spine",
    "nav",
    "internal_links",
    "anchors",
    "footnotes",
    "assets",
    "placeholders",
    "parse",
    "bilingual_source",
)


def compare_source_models(
    source_model: dict[str, Any],
    output_model: dict[str, Any],
    source_assets: dict[str, dict[str, Any]],
    output_assets: dict[str, dict[str, Any]],
    failures: list[dict[str, str]],
) -> None:
    sm = source_model["model"]
    om = output_model["model"]
    source_by_path = {entry["path"]: entry for entry in sm["resolved"]}
    output_by_path = {entry["path"]: entry for entry in om["resolved"]}
    output_by_id = {entry["id"]: entry for entry in om["resolved"] if entry["id"]}
    for path, source_item in sorted(source_by_path.items()):
        other = output_by_path.get(path) or output_by_id.get(source_item["id"])
        if other is None:
            failures.append(
                archive_model.item("resources", "missing_manifest_resource", path, "source")
            )
            continue
        for field in ("media", "properties", "fallback", "media_overlay"):
            if source_item[field] != other[field]:
                failures.append(
                    archive_model.item("resources", "manifest_metadata_mismatch", path, field)
                )
        if source_item["path"] != other["path"]:
            failures.append(
                archive_model.item("resources", "manifest_metadata_mismatch", path, "href")
            )
    source_item_keys = {(entry["id"], entry["href"], entry["media"]) for entry in sm["items"]}
    for output_item in om["items"]:
        key = (output_item["id"], output_item["href"], output_item["media"])
        if key not in source_item_keys:
            failures.append(
                archive_model.item(
                    "resources",
                    "extra_manifest_resource",
                    output_item["id"] or "manifest",
                    "output",
                )
            )
    for path in sorted(set(output_by_path) - set(source_by_path)):
        failures.append(archive_model.item("resources", "extra_manifest_resource", path, "output"))
    if sm["spine_paths"] != om["spine_paths"]:
        failures.append(archive_model.item("spine", "sequence_mismatch", "<spine>", "source"))
    source_nav = sorted(entry["path"] for entry in sm["nav_items"])
    output_nav = sorted(entry["path"] for entry in om["nav_items"])
    source_ncx = sorted(entry["path"] for entry in sm["ncx_items"])
    output_ncx = sorted(entry["path"] for entry in om["ncx_items"])
    if source_nav != output_nav or source_ncx != output_ncx:
        failures.append(archive_model.item("nav", "declaration_mismatch", "<manifest>", "toc"))
    if source_nav and not output_nav:
        failures.append(archive_model.item("nav", "missing_source_nav", "<manifest>", "source"))
    if source_ncx and not output_ncx:
        failures.append(archive_model.item("nav", "missing_source_ncx", "<manifest>", "source"))
    source_members = {name for name in source_model["archive"] if not name.endswith("/")}
    output_members = {name for name in output_model["archive"] if not name.endswith("/")}
    for name in sorted(source_members - output_members):
        failures.append(archive_model.item("resources", "missing_resource", name, "source"))
    for name in sorted(output_members - source_members):
        failures.append(archive_model.item("resources", "unmanifested_resource", name, "output"))
    for asset in sorted(set(source_assets) | set(output_assets)):
        if asset not in output_assets:
            failures.append(archive_model.item("assets", "missing_asset", asset, "missing"))
        elif asset not in source_assets:
            failures.append(archive_model.item("assets", "extra_asset", asset, "extra"))
        elif source_assets[asset]["sha256"] != output_assets[asset]["sha256"]:
            failures.append(archive_model.item("assets", "changed_asset", asset, "changed"))


def finish(
    result: dict[str, Any],
    failures: list[dict[str, str]],
    warnings: list[dict[str, str]],
    checked: dict[str, int],
) -> dict[str, Any]:
    failures = archive_model.sort_items(failures)
    warnings = archive_model.sort_items(warnings)
    result["failures"] = failures
    result["warnings"] = warnings
    result["structural_pass"] = not failures
    result["counts"] = {
        category: {
            "checked": checked.get(category, 0),
            "failures": sum(1 for entry in failures if entry["category"] == category),
            "warnings": sum(1 for entry in warnings if entry["category"] == category),
        }
        for category in _CATEGORIES
    }
    result["generated_resources"] = sorted(set(result.get("generated_resources", [])))
    return result
