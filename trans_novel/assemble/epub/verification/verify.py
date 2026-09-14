"""EPUB evidence and verification orchestration."""

from __future__ import annotations

import os
import re
import zipfile
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from lxml import etree

from trans_novel.assemble.epub.metadata import epub_language
from trans_novel.assemble.epub.rendering.theme.contracts import (
    NotePathMapping,
    ThemeError,
    ThemePlan,
)
from trans_novel.assemble.epub.verification import archive_model, preservation, validation
from trans_novel.assemble.epub.verification import slots as slot
from trans_novel.assemble.epub.verification.theme import theme_projection, theme_summary
from trans_novel.epub.package import HTML_MEDIA, NCX_MEDIA, read_package

_REPORT_DETAILS = {
    "active",
    "archive",
    "atomic",
    "changed",
    "count_mismatch",
    "digest",
    "empty",
    "empty_body",
    "fsync",
    "href",
    "html",
    "immutable",
    "invalid",
    "malformed",
    "malformed XML",
    "manifest",
    "media",
    "member",
    "missing",
    "mono",
    "navMap",
    "navPoint",
    "ncx",
    "not_source_segment",
    "ol",
    "opf",
    "output",
    "pair_mismatch",
    "recovered",
    "schema4",
    "schema4_locator",
    "schema4_required",
    "source",
    "source_mode",
    "source_unreadable",
    "spine has no content",
    "src",
    "state",
    "strict_required",
    "target",
    "toc",
    "unattached",
    "unexpected",
    "unreadable",
    "unsafe",
    "unsupported",
    "writer",
    "xhtml",
    "container.xml is required",
    "rootfile unresolved",
    "li",
    "a_or_span",
}
SCHEMA_VERSION = 1
CATEGORIES = validation.CATEGORIES


def output_label(path: str | os.PathLike[str]) -> str:
    name = os.path.basename(os.fspath(path))
    name = re.sub(r"[\\/:*?\"<>|\r\n\t]+", " ", name).strip().strip(".")
    name = re.sub(r"\s+", " ", name)[:120]
    return name or "translated.epub"


def report_item(item: dict[str, str]) -> dict[str, str]:
    """Convert validator evidence to fixed, privacy-safe report vocabulary."""
    detail = str(item.get("detail", "invalid"))
    return {
        "category": str(item.get("category", "resources")),
        "code": str(item.get("code", "invalid")),
        "path": archive_model.archive_label(str(item.get("path", "<output>"))),
        "detail": detail if detail in _REPORT_DETAILS else "invalid",
    }


def state_resources(store: Any) -> tuple[dict[str, Any], list[Any], list[dict[str, str]]]:
    """Reload manifest and chapters, never borrowing writer-owned objects."""
    failures: list[dict[str, str]] = []
    try:
        manifest = store.load_manifest()
        chapters = [store.load_chapter(int(entry["index"])) for entry in manifest["chapters"]]
    except Exception:
        return {}, [], [archive_model.item("state", "state_unreadable", "<state>", "unreadable")]
    meta = manifest.get("meta") if isinstance(manifest.get("meta"), dict) else {}
    schema = meta.get("epub_schema")
    if schema != 4:
        failures.append(
            archive_model.item("state", "unsupported_schema", "<state>", "schema4_required")
        )
    resources = {
        str(item.get("href")): item
        for item in meta.get("epub_resources", [])
        if isinstance(item, dict) and isinstance(item.get("href"), str)
    }
    return resources, chapters, failures


def _check_package(
    source: Path,
    output: Path,
    mode: str,
    target_lang: str | None,
    differences: dict[str, int],
    failures: list[dict[str, str]],
) -> None:
    if mode in {"monolingual", "bilingual"} and source is not None:
        try:
            with (
                zipfile.ZipFile(source, "r") as source_zip,
                zipfile.ZipFile(output, "r") as output_zip,
            ):
                source_info = read_package(source_zip, [])
                output_info = read_package(output_zip, [])
                source_opf = source_info.get("opf_path")
                output_opf = output_info.get("opf_path")
                if source_opf and output_opf and source_opf == output_opf:
                    source_root = etree.fromstring(
                        archive_model.read_member(source_zip, source_zip.getinfo(source_opf))
                    )
                    output_root = etree.fromstring(
                        archive_model.read_member(output_zip, output_zip.getinfo(output_opf))
                    )
                    language_seen = False
                    opf_language_changed = 0

                    def compare_package(left: etree._Element, right: etree._Element) -> bool:
                        nonlocal language_seen, opf_language_changed
                        if left.tag != right.tag or dict(left.attrib) != dict(right.attrib):
                            return False
                        is_language = left.tag == "{http://purl.org/dc/elements/1.1/}language"
                        first_language = is_language and not language_seen
                        if is_language:
                            language_seen = True
                        if first_language:
                            if target_lang and right.text != target_lang:
                                return False
                            if left.text != right.text:
                                opf_language_changed += 1
                        elif left.text != right.text:
                            return False
                        if left.tail != right.tail:
                            return False
                        if len(left) != len(right):
                            return False
                        return all(compare_package(a, b) for a, b in zip(left, right, strict=True))

                    if not compare_package(source_root, output_root):
                        failures.append(
                            archive_model.item(
                                "resources", "package_metadata_mismatch", source_opf, "opf"
                            )
                        )
                    differences["language_fields"] += opf_language_changed
        except (OSError, zipfile.BadZipFile, etree.XMLSyntaxError):
            failures.append(
                archive_model.item("resources", "package_unreadable", "<package>", "opf")
            )


def _check_source_archive(
    source: Path,
    output: Path,
    mode: str,
    store: Any | None,
    failures: list[dict[str, str]],
) -> None:
    if mode in {"monolingual", "bilingual"} and source is not None:
        try:
            with (
                zipfile.ZipFile(source, "r") as source_zip,
                zipfile.ZipFile(output, "r") as output_zip,
            ):
                source_infos = source_zip.infolist()
                output_infos = output_zip.infolist()
                source_names = [info.filename for info in source_infos]
                output_names = [info.filename for info in output_infos]
                expected_output_names = ["mimetype"] + [
                    name for name in source_names if name != "mimetype"
                ]
                if output_names != expected_output_names:
                    failures.append(
                        archive_model.item("zip", "member_set_mismatch", "<archive>", "source")
                    )
                elif len(source_names) != len(set(source_names)):
                    failures.append(
                        archive_model.item("zip", "duplicate_source_member", "<archive>", "source")
                    )
                else:
                    output_by_name = {info.filename: info for info in output_infos}
                    for source_info in source_infos:
                        output_info = output_by_name[source_info.filename]
                        metadata = (
                            "compress_type",
                            "date_time",
                            "external_attr",
                            "internal_attr",
                            "extra",
                            "comment",
                        )
                        if source_info.filename == "mimetype":
                            metadata = tuple(key for key in metadata if key != "compress_type")
                        flags_match = source_info.flag_bits == output_info.flag_bits
                        if (
                            any(
                                getattr(source_info, key) != getattr(output_info, key)
                                for key in metadata
                            )
                            or not flags_match
                        ):
                            failures.append(
                                archive_model.item(
                                    "zip",
                                    "member_metadata_mismatch",
                                    source_info.filename,
                                    "source",
                                )
                            )
                            break
        except (OSError, zipfile.BadZipFile):
            failures.append(archive_model.item("zip", "source_unreadable", "<source>", "archive"))
        try:
            with (
                zipfile.ZipFile(source, "r") as source_zip,
                zipfile.ZipFile(output, "r") as output_zip,
            ):
                source_archive_info = read_package(source_zip, [])
                output_info = {info.filename: info for info in output_zip.infolist()}
                toc_paths = {
                    item["path"]
                    for item in source_archive_info.get("model", {}).get("nav_items", [])
                    + source_archive_info.get("model", {}).get("ncx_items", [])
                }
                if store is not None:
                    try:
                        persisted_toc = store.load_manifest().get("meta", {}).get("toc_entries", [])
                    except Exception:
                        persisted_toc = []
                    toc_paths.update(
                        entry.get("toc_path")
                        for entry in persisted_toc
                        if isinstance(entry, dict) and isinstance(entry.get("toc_path"), str)
                    )
                authorized_names = set(toc_paths)
                source_opf = source_archive_info.get("opf_path")
                if isinstance(source_opf, str):
                    authorized_names.add(source_opf)
                authorized_names.update(
                    item["path"]
                    for item in source_archive_info.get("model", {}).get("resolved", [])
                    if item.get("media") in HTML_MEDIA
                    or item.get("media") == NCX_MEDIA
                    or "nav" in item.get("properties", "").split()
                )
                if source_zip.comment != output_zip.comment:
                    failures.append(
                        archive_model.item("zip", "archive_comment_mismatch", "<archive>", "source")
                    )
                for info in source_zip.infolist():
                    name = info.filename
                    if name not in output_info or name == "mimetype" or name in authorized_names:
                        continue
                    if archive_model.read_member(source_zip, info) != archive_model.read_member(
                        output_zip, output_info[name]
                    ):
                        failures.append(
                            archive_model.item("assets", "changed_asset", name, "changed")
                        )
        except (OSError, zipfile.BadZipFile):
            failures.append(
                archive_model.item("assets", "source_unreadable", "<source>", "archive")
            )


def _new_structural_failures(
    output_failures: list[dict[str, str]],
    source_failures: list[dict[str, str]],
    *,
    state_backed_bilingual: bool = False,
) -> list[dict[str, str]]:
    superseded_bilingual_codes = {
        "source_node_count",
        "source_node_misplaced",
        "source_node_unexpected",
    }
    inherited = Counter(tuple(sorted(item.items())) for item in source_failures)
    result: list[dict[str, str]] = []
    for item in output_failures:
        if state_backed_bilingual and item.get("code") in superseded_bilingual_codes:
            continue
        key = tuple(sorted(item.items()))
        if inherited[key]:
            inherited[key] -= 1
            continue
        result.append(item)
    return result


def _verify_epub(
    output_path: str | os.PathLike[str],
    *,
    source_path: str | os.PathLike[str] | None = None,
    store: Any | None = None,
    mode: str = "generated",
    bilingual: bool = False,
    target_lang: str | None = None,
    bilingual_order: str = "target_first",
    note_mappings: Mapping[str, tuple[NotePathMapping, ...]] | None = None,
) -> dict[str, Any]:
    """Reopen an on-disk EPUB and return deterministic report v1 evidence."""
    output = Path(output_path)
    source = Path(source_path) if source_path is not None else None
    source_sha = archive_model.sha256(source) if source is not None else None
    failures: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    checked = dict.fromkeys(CATEGORIES, 0)
    differences = {"text_slots": 0, "toc_labels": 0, "language_fields": 0, "bilingual_nodes": 0}
    resources: dict[str, Any] = {}
    chapters: list[Any] = []
    if mode in {"monolingual", "bilingual"}:
        if store is None or source is None:
            failures.append(archive_model.item("state", "state_required", "<state>", "source_mode"))
        else:
            resources, chapters, state_failures = state_resources(store)
            failures.extend(state_failures)
            try:
                persisted_sha = store.load_manifest().get("meta", {}).get("epub_sha256")
                if persisted_sha != source_sha:
                    failures.append(
                        archive_model.item("state", "source_digest_mismatch", "<source>", "schema4")
                    )
            except Exception:
                failures.append(
                    archive_model.item("state", "state_unreadable", "<state>", "digest")
                )
    if target_lang is None and store is not None:
        try:
            target_lang = epub_language(store.load_manifest().get("target_lang"))
        except Exception:
            target_lang = None
    structural_bilingual = bilingual
    if mode == "generated" and bilingual and store is not None:
        try:
            manifest_chapters = store.load_manifest().get("chapters", [])
            if manifest_chapters and all(
                store.load_chapter(meta["index"]).preserve_source for meta in manifest_chapters
            ):
                structural_bilingual = False
        except Exception:
            pass
    structural = validation.validate_one(
        output,
        source_path=source if mode in {"monolingual", "bilingual"} else None,
        bilingual=structural_bilingual,
    )
    source_failures = (
        validation.validate_one(source, source_path=None, bilingual=None).get("failures", [])
        if mode in {"monolingual", "bilingual"} and source is not None
        else []
    )
    _check_source_archive(source, output, mode, store, failures)
    _check_package(source, output, mode, target_lang, differences, failures)
    failures.extend(
        _new_structural_failures(
            structural.get("failures", []),
            source_failures,
            state_backed_bilingual=mode == "bilingual" and store is not None and source is not None,
        )
    )
    warnings.extend(structural.get("warnings", []))
    checked.update(
        {
            category: int(structural.get("counts", {}).get(category, {}).get("checked", 0))
            for category in CATEGORIES
        }
    )
    if mode in {"monolingual", "bilingual"} and source is not None and store is not None:
        slot_differences = slot.slot_proof(
            source,
            output,
            store,
            resources,
            chapters,
            bilingual=bilingual,
            target_lang=target_lang,
            bilingual_order=bilingual_order,
            failures=failures,
            warnings=warnings,
            checked=checked,
            note_mappings=note_mappings,
        )
        for key, value in slot_differences.items():
            differences[key] += value
    if mode == "generated" and store is not None:
        preservation.generated_chapter_proof(output, store, failures, checked)
    failures = archive_model.sort_items([report_item(item) for item in failures])
    warnings = archive_model.sort_items([report_item(item) for item in warnings])
    assurance = "verified"
    if any(str(item.get("parse_mode")) == "recovered" for item in resources.values()):
        assurance = "recovered"
    return {
        "schema_version": 1,
        "mode": mode,
        "assurance": assurance,
        "passed": not failures,
        "published": False,
        "source_sha256": source_sha,
        "output_sha256": archive_model.sha256(output),
        "output_label": output_label(output),
        "failures": failures,
        "warnings": warnings,
        "checked": {category: int(checked.get(category, 0)) for category in CATEGORIES},
        "authorized_differences": differences,
    }


def _theme_failure_report(
    output: Path,
    source: Path | None,
    mode: str,
    error: ThemeError,
) -> dict[str, Any]:
    failure = report_item(
        archive_model.item("resources", "theme_verify", error.resource or "<archive>", "invalid")
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": mode,
        "assurance": "verified",
        "passed": False,
        "published": False,
        "source_sha256": archive_model.sha256(source) if source is not None else None,
        "output_sha256": archive_model.sha256(output),
        "output_label": output_label(output),
        "failures": [failure],
        "warnings": [],
        "checked": dict.fromkeys(CATEGORIES, 0),
        "authorized_differences": {
            "text_slots": 0,
            "toc_labels": 0,
            "language_fields": 0,
            "bilingual_nodes": 0,
        },
        "theme": None,
    }


def verify_epub(
    output_path: str | os.PathLike[str],
    *,
    source_path: str | os.PathLike[str] | None = None,
    store: Any | None = None,
    mode: str = "generated",
    bilingual: bool = False,
    target_lang: str | None = None,
    bilingual_order: str = "target_first",
    theme_plan: ThemePlan | None = None,
) -> dict[str, Any]:
    """通过私有可逆计划投影核验实际输出。"""
    output = Path(output_path)
    source = Path(source_path) if source_path is not None else None
    try:
        if theme_plan is not None and theme_plan.bilingual is not bilingual:
            raise ThemeError("theme_verify", "invalid_plan")
        note_mappings = (
            {
                resource.resource_href: resource.scope.note_paths
                for resource in theme_plan.resources
                if resource.note_changes
            }
            if theme_plan is not None
            else {}
        )
        state_backed_note_proof = (
            store
            if bilingual and source is not None and mode in {"monolingual", "bilingual"}
            else None
        )
        with theme_projection(
            output,
            theme_plan,
            source_path=source,
            store=state_backed_note_proof,
            target_lang=target_lang,
            bilingual_order=bilingual_order,
        ) as projected:
            report = _verify_epub(
                projected,
                source_path=source,
                store=store,
                mode=mode,
                bilingual=bilingual,
                target_lang=target_lang,
                bilingual_order=bilingual_order,
                note_mappings=note_mappings if bilingual else None,
            )
    except ThemeError as error:
        return _theme_failure_report(output, source, mode, error)
    report["output_sha256"] = archive_model.sha256(output)
    report["output_label"] = output_label(output)
    report["theme"] = theme_summary(theme_plan) if theme_plan is not None else None
    return report
