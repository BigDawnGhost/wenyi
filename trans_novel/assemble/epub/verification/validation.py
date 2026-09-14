"""Bounded EPUB structural validation and triplet validation."""

from __future__ import annotations

import zipfile
from collections import Counter
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from trans_novel.assemble.epub.rendering.theme.contracts import ThemeError, ThemePlan
from trans_novel.assemble.epub.verification import archive_compare as compare
from trans_novel.assemble.epub.verification import archive_model, dom, package, structure
from trans_novel.assemble.epub.verification.theme import theme_projection, theme_summary
from trans_novel.epub.package import HTML_MEDIA, NCX_MEDIA, read_package

SCHEMA_VERSION = 1
CATEGORIES = (
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
MAX_MEMBER_BYTES = archive_model.MAX_MEMBER_BYTES
MAX_ARCHIVE_BYTES = archive_model.MAX_ARCHIVE_BYTES
MAX_ARCHIVE_MEMBERS = archive_model.MAX_ARCHIVE_MEMBERS


def _validate_content(
    zf: Any,
    model_info: dict[str, Any],
    model: dict[str, Any],
    archive: set[str],
    failures: list[dict[str, str]],
    warnings: list[dict[str, str]],
    checked: dict[str, int],
    bilingual: bool | None,
    result: dict[str, Any],
) -> tuple[dict[str, BeautifulSoup], dict[str, set[str]], Counter, Counter]:
    opf_path = model_info["opf_path"]
    content_items = [
        entry
        for entry in model["resolved"]
        if entry["media"] in HTML_MEDIA and "nav" not in entry["properties"].split()
    ]
    toc_items = model["nav_items"] + model["ncx_items"]
    soups: dict[str, BeautifulSoup] = {}
    ids_by_path: dict[str, set[str]] = {}
    for entry in content_items + toc_items:
        content_path = entry["path"]
        if content_path in soups:
            continue
        data = archive_model.model_read(zf, content_path, failures)
        if data is None:
            continue
        soup, valid = structure.html_soup(data, entry["media"])
        checked["parse"] += 1
        if not valid:
            failures.append(
                archive_model.item(
                    "parse",
                    "malformed_content" if entry["media"] in HTML_MEDIA else "invalid_toc",
                    content_path,
                    "malformed",
                )
            )
        soups[content_path] = soup
        ids_by_path[content_path] = structure.ids(soup)
        structure.check_document_features(
            soup, content_path, failures, checked, content=entry in content_items
        )
    if not model["nav_items"] and not model["ncx_items"]:
        checked["nav"] += 1
        failures.append(archive_model.item("nav", "missing_toc", opf_path, "toc"))
    else:
        checked["nav"] += len(model["nav_items"]) + len(model["ncx_items"])
    nav_graph = Counter()
    for entry in model["nav_items"] + model["ncx_items"]:
        soup = soups.get(entry["path"])
        if soup is None:
            continue
        if entry["media"] == NCX_MEDIA:
            structure.check_ncx_semantics(soup, entry["path"], failures, checked)
        elif entry["media"] in HTML_MEDIA:
            structure.check_nav_semantics(
                soup,
                entry["path"],
                failures,
                checked,
                allow_typeless="nav" in entry["properties"].split(),
            )
        nav_graph.update(
            structure.check_links(
                soup,
                entry["path"],
                archive,
                ids_by_path,
                failures,
                warnings,
                checked,
                category="nav",
            )
        )
    content_paths = {entry["path"] for entry in content_items}
    current_graph = structure.graph_from_soups(
        {resource: soups[resource] for resource in soups if resource in content_paths},
        archive,
        ids_by_path,
        failures,
        warnings,
        checked,
    )
    current_graph.update(nav_graph)
    structure.check_footnotes(soups, ids_by_path, failures, warnings, checked)
    current_assets = archive_model.resource_hashes(
        zf, model_info | {"model": model}, opf_path, failures, checked
    )
    return soups, ids_by_path, current_graph, current_assets


def validate_one(
    path: Path,
    *,
    source_path: Path | None,
    bilingual: bool | None,
    max_member_bytes: int = MAX_MEMBER_BYTES,
    max_archive_bytes: int = MAX_ARCHIVE_BYTES,
) -> dict[str, Any]:
    failures: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    checked = dict.fromkeys(CATEGORIES, 0)
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "path_sha256": archive_model.sha256(path),
        "structural_pass": False,
        "counts": {
            category: {"checked": 0, "failures": 0, "warnings": 0} for category in CATEGORIES
        },
        "failures": [],
        "warnings": [],
        "generated_resources": [],
    }
    zf, _infos, archive_names = archive_model.open_validated_zip(
        path,
        failures,
        checked,
        max_member_bytes=max_member_bytes,
        max_archive_bytes=max_archive_bytes,
    )
    if zf is None:
        return compare.finish(result, failures, warnings, checked)
    with zf:
        archive = archive_names
        model_info, model = package.read_package(zf, archive, failures, checked)
        if model_info is None or model is None:
            return compare.finish(result, failures, warnings, checked)
        opf_path = model_info["opf_path"]
        package.check_manifest_resources(model, opf_path, archive, failures, checked)
        soups, _ids_by_path, current_graph, current_assets = _validate_content(
            zf, model_info, model, archive, failures, warnings, checked, bilingual, result
        )
        if source_path is not None:
            package.compare_source_archive(
                source_path,
                model_info,
                model,
                current_assets,
                current_graph,
                soups,
                bilingual,
                checked,
                failures,
            )
        if bilingual is not None:
            if bilingual:
                validate_bilingual_nodes(soups, failures, checked)
                if source_path is not None and source_path.is_file():
                    dom.source_subset(path, source_path, soups, failures, checked)
            else:
                checked["bilingual_source"] += 1
                if any(soup.select(".tn-source") for soup in soups.values()):
                    failures.append(
                        archive_model.item(
                            "bilingual_source", "unexpected_source_nodes", "<output>", "unexpected"
                        )
                    )
            try:
                from trans_novel.ingest.epub.reader import read_epub

                reopened = read_epub(str(path), "en", "zh")
                checked["bilingual_source"] += 1
                if not reopened.chapters or not any(ch.text_segments for ch in reopened.chapters):
                    failures.append(
                        archive_model.item("bilingual_source", "reopen_empty", "<output>", "empty")
                    )
            except Exception:
                failures.append(
                    archive_model.item(
                        "bilingual_source", "reopen_failed", "<output>", "unreadable"
                    )
                )
    return compare.finish(result, failures, warnings, checked)


def validate_epub(
    path: Path,
    *,
    source_path: Path | None = None,
    bilingual: bool | None = None,
    max_member_bytes: int = MAX_MEMBER_BYTES,
    max_archive_bytes: int = MAX_ARCHIVE_BYTES,
) -> dict[str, Any]:
    return validate_one(
        Path(path),
        source_path=Path(source_path) if source_path is not None else None,
        bilingual=bilingual,
        max_member_bytes=max_member_bytes,
        max_archive_bytes=max_archive_bytes,
    )


def _triplet_result(
    source: dict[str, Any], mono: dict[str, Any], bilingual: dict[str, Any]
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "structural_pass": bool(
            source["structural_pass"] and mono["structural_pass"] and bilingual["structural_pass"]
        ),
        "source": source,
        "mono": mono,
        "bilingual": bilingual,
    }


def _theme_failure(path: Path, error: ThemeError) -> dict[str, Any]:
    checked = dict.fromkeys(CATEGORIES, 0)
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "path_sha256": archive_model.sha256(path),
        "structural_pass": False,
        "counts": {},
        "failures": [],
        "warnings": [],
        "generated_resources": [],
        "theme": None,
    }
    failure = archive_model.item(
        "resources", "theme_verify", error.resource or "<archive>", "invalid"
    )
    return compare.finish(result, [failure], [], checked)


def _validate_expected_one(
    path: Path,
    plan: ThemePlan | None,
    *,
    expected_bilingual: bool,
    source_path: Path,
    max_member_bytes: int,
    max_archive_bytes: int,
    store: Any | None,
    target_lang: str | None,
    bilingual_order: str,
) -> dict[str, Any]:
    try:
        if plan is not None and plan.bilingual is not expected_bilingual:
            raise ThemeError("theme_verify", "invalid_plan")
        with theme_projection(
            path,
            plan,
            source_path=source_path,
            store=store,
            target_lang=target_lang,
            bilingual_order=bilingual_order,
            max_member_bytes=max_member_bytes,
            max_archive_bytes=max_archive_bytes,
        ) as projected:
            result = validate_epub(
                projected,
                source_path=source_path,
                bilingual=expected_bilingual,
                max_member_bytes=max_member_bytes,
                max_archive_bytes=max_archive_bytes,
            )
    except ThemeError as error:
        return _theme_failure(path, error)
    result["path_sha256"] = archive_model.sha256(path)
    result["theme"] = theme_summary(plan) if plan is not None else None
    return result


def _validate_epub_triplet_projected(
    source_path: Path,
    mono_path: Path,
    bilingual_path: Path,
    *,
    actual_mono_path: Path,
    actual_bilingual_path: Path,
    mono_theme_plan: ThemePlan | None,
    bilingual_theme_plan: ThemePlan | None,
    max_member_bytes: int,
    max_archive_bytes: int,
) -> dict[str, Any]:
    source = validate_epub(
        source_path,
        max_member_bytes=max_member_bytes,
        max_archive_bytes=max_archive_bytes,
    )
    mono = validate_epub(
        mono_path,
        source_path=source_path,
        bilingual=False,
        max_member_bytes=max_member_bytes,
        max_archive_bytes=max_archive_bytes,
    )
    bilingual = validate_epub(
        bilingual_path,
        source_path=source_path,
        bilingual=True,
        max_member_bytes=max_member_bytes,
        max_archive_bytes=max_archive_bytes,
    )
    proof_failures: list[dict[str, str]] = []
    proof_checked = dict.fromkeys(CATEGORIES, 0)
    if source_path.is_file() and mono_path.is_file() and bilingual_path.is_file():
        try:
            with zipfile.ZipFile(bilingual_path, "r") as zf:
                info = read_package(zf, proof_failures)
                model = info["model"]
                soups: dict[str, BeautifulSoup] = {}
                for item in model["resolved"]:
                    if item["media"] not in HTML_MEDIA or "nav" in item["properties"].split():
                        continue
                    data = archive_model.model_read(zf, item["path"], proof_failures)
                    if data is not None:
                        soups[item["path"]] = structure.html_soup(data, item["media"])[0]
            dom.exact_bilingual_proof(source_path, mono_path, soups, proof_failures, proof_checked)
        except (OSError, zipfile.BadZipFile):
            proof_failures.append(
                archive_model.item("bilingual_source", "proof_unreadable", "<output>", "unreadable")
            )
    bilingual["failures"].extend(proof_failures)
    bilingual = compare.finish(
        bilingual,
        bilingual["failures"],
        bilingual["warnings"],
        {
            category: bilingual["counts"][category]["checked"] + proof_checked.get(category, 0)
            for category in CATEGORIES
        },
    )
    mono["path_sha256"] = archive_model.sha256(actual_mono_path)
    bilingual["path_sha256"] = archive_model.sha256(actual_bilingual_path)
    mono["theme"] = theme_summary(mono_theme_plan) if mono_theme_plan is not None else None
    bilingual["theme"] = (
        theme_summary(bilingual_theme_plan) if bilingual_theme_plan is not None else None
    )
    return _triplet_result(source, mono, bilingual)


def validate_epub_triplet(
    source_path: Path,
    mono_path: Path,
    bilingual_path: Path,
    *,
    mono_theme_plan: ThemePlan | None = None,
    bilingual_theme_plan: ThemePlan | None = None,
    store: Any | None = None,
    target_lang: str | None = None,
    bilingual_order: str = "target_first",
    max_member_bytes: int = MAX_MEMBER_BYTES,
    max_archive_bytes: int = MAX_ARCHIVE_BYTES,
) -> dict[str, Any]:
    source_path = Path(source_path)
    mono_path = Path(mono_path)
    bilingual_path = Path(bilingual_path)
    if mono_theme_plan is not None and mono_theme_plan.bilingual is not False:
        mono = _theme_failure(mono_path, ThemeError("theme_verify", "invalid_plan"))
        bilingual = _validate_expected_one(
            bilingual_path,
            bilingual_theme_plan,
            expected_bilingual=True,
            source_path=source_path,
            store=store,
            target_lang=target_lang,
            bilingual_order=bilingual_order,
            max_member_bytes=max_member_bytes,
            max_archive_bytes=max_archive_bytes,
        )
        source = validate_epub(
            source_path,
            max_member_bytes=max_member_bytes,
            max_archive_bytes=max_archive_bytes,
        )
        return _triplet_result(source, mono, bilingual)
    try:
        with theme_projection(
            mono_path,
            mono_theme_plan,
            source_path=source_path,
            store=store,
            target_lang=target_lang,
            bilingual_order=bilingual_order,
            max_member_bytes=max_member_bytes,
            max_archive_bytes=max_archive_bytes,
        ) as projected_mono:
            try:
                if bilingual_theme_plan is not None and bilingual_theme_plan.bilingual is not True:
                    raise ThemeError("theme_verify", "invalid_plan")
                with theme_projection(
                    bilingual_path,
                    bilingual_theme_plan,
                    source_path=source_path,
                    store=store,
                    target_lang=target_lang,
                    bilingual_order=bilingual_order,
                    max_member_bytes=max_member_bytes,
                    max_archive_bytes=max_archive_bytes,
                ) as projected_bilingual:
                    return _validate_epub_triplet_projected(
                        source_path,
                        projected_mono,
                        projected_bilingual,
                        actual_mono_path=mono_path,
                        actual_bilingual_path=bilingual_path,
                        mono_theme_plan=mono_theme_plan,
                        bilingual_theme_plan=bilingual_theme_plan,
                        max_member_bytes=max_member_bytes,
                        max_archive_bytes=max_archive_bytes,
                    )
            except ThemeError as error:
                source = validate_epub(
                    source_path,
                    max_member_bytes=max_member_bytes,
                    max_archive_bytes=max_archive_bytes,
                )
                mono = validate_epub(
                    projected_mono,
                    source_path=source_path,
                    bilingual=False,
                    max_member_bytes=max_member_bytes,
                    max_archive_bytes=max_archive_bytes,
                )
                mono["path_sha256"] = archive_model.sha256(mono_path)
                mono["theme"] = (
                    theme_summary(mono_theme_plan) if mono_theme_plan is not None else None
                )
                return _triplet_result(source, mono, _theme_failure(bilingual_path, error))
    except ThemeError as error:
        source = validate_epub(
            source_path,
            max_member_bytes=max_member_bytes,
            max_archive_bytes=max_archive_bytes,
        )
        bilingual = _validate_expected_one(
            bilingual_path,
            bilingual_theme_plan,
            expected_bilingual=True,
            source_path=source_path,
            store=store,
            target_lang=target_lang,
            bilingual_order=bilingual_order,
            max_member_bytes=max_member_bytes,
            max_archive_bytes=max_archive_bytes,
        )
        return _triplet_result(source, _theme_failure(mono_path, error), bilingual)


def validate_bilingual_nodes(
    soups: dict[str, BeautifulSoup],
    failures: list[dict[str, str]],
    checked: dict[str, int],
) -> None:
    """Require nonempty source nodes attached to adjacent translated blocks."""
    total = sum(len(soup.select(".tn-source")) for soup in soups.values())
    if total == 0:
        failures.append(
            archive_model.item("bilingual_source", "missing_source_nodes", "<output>", "missing")
        )
    for resource, soup in soups.items():
        for node in soup.select(".tn-source"):
            checked["bilingual_source"] += 1
            if not dom.norm_text(node.get_text("", strip=False)):
                failures.append(
                    archive_model.item("bilingual_source", "source_node_empty", resource, "empty")
                )
                continue
            if not dom.source_node_attached(node):
                failures.append(
                    archive_model.item(
                        "bilingual_source", "source_node_misplaced", resource, "unattached"
                    )
                )
