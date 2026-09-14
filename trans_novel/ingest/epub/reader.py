"""Read EPUB sources into schema-4 structural text-slot state."""

from __future__ import annotations

import hashlib
import os
import zipfile
from collections.abc import Iterable
from typing import Any, Literal

from lxml import etree

from trans_novel.epub.archive import ZipSafetyError, preflight_zip, read_member
from trans_novel.epub.markup import resource_parser
from trans_novel.epub.navigation import parse_nav_landmarks, parse_toc_entries
from trans_novel.epub.notes import detect_note_relations
from trans_novel.epub.package import HTML_MEDIA, read_package
from trans_novel.ingest.epub.chapters import logical_chapters
from trans_novel.ingest.epub.markup import annotate_resource
from trans_novel.ingest.models import Chapter, Document

_ParsedResource = tuple[int, str, bytes, etree._ElementTree, str, list[dict[str, object]]]


def _spine_paths(model: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for item_id in model["spine_ids"]:
        matching = [item for item in model["resolved"] if item["id"] == item_id]
        if len(matching) != 1:
            raise ValueError(f"EPUB spine rejected: unresolved_idref ({item_id})")
        item = matching[0]
        if item["media"] not in HTML_MEDIA:
            raise ValueError(f"EPUB spine rejected: non_content_item ({item_id}: {item['media']})")
        paths.append(item["path"])
    return list(dict.fromkeys(paths))


def _semantic_hints_by_resource(
    guide_entries: list[dict[str, Any]], landmarks: list[dict[str, Any]]
) -> dict[str, list[str]]:
    hints: dict[str, list[str]] = {}
    for prefix, entries in (("opf:guide", guide_entries), ("nav:landmark", landmarks)):
        for entry in entries:
            href = entry.get("resource_href")
            if not isinstance(href, str) or not href:
                continue
            values = [
                f"{prefix}:{name}={entry[name]}"
                for name in ("nav_type", "type", "role")
                if isinstance(entry.get(name), str) and entry[name]
            ]
            hints.setdefault(href, []).extend(values)
    return hints


def _note_resources_by_semantics(
    guide_entries: list[dict[str, Any]], landmarks: list[dict[str, Any]]
) -> dict[str, Literal["footnote", "endnote"]]:
    evidence: dict[str, set[Literal["footnote", "endnote"]]] = {}
    for entry in [*guide_entries, *landmarks]:
        href = entry.get("resource_href")
        if not isinstance(href, str) or not href:
            continue
        tokens = {
            token
            for name in ("type", "nav_type", "role")
            if isinstance(entry.get(name), str)
            for token in entry[name].split()
        }
        if tokens & {"notes", "footnotes", "doc-footnote"}:
            evidence.setdefault(href, set()).add("footnote")
        if tokens & {"endnotes", "doc-endnotes"}:
            evidence.setdefault(href, set()).add("endnote")
    return {href: next(iter(kinds)) for href, kinds in evidence.items() if len(kinds) == 1}


def read_epub(path: str, source_lang: str, target_lang: str) -> Document:
    """Read a source EPUB into schema-4 structural text-slot state."""
    try:
        with zipfile.ZipFile(path, "r") as zf:
            preflight_zip(zf)
            failures: list[dict[str, str]] = []
            package = read_package(zf, failures)
            if failures:
                first = failures[0]
                raise ValueError(f"EPUB package rejected: {first['code']} ({first['path']})")
            opf_path = package["opf_path"]
            model = package["model"]
            book_title = model["title"]
            hrefs = _spine_paths(model)
            toc_paths = model["toc_paths"]
            toc_entries = parse_toc_entries(zf, model["toc_kinds"])
            landmarks = parse_nav_landmarks(zf, [str(item["path"]) for item in model["nav_items"]])
            resource_hints = _semantic_hints_by_resource(model["guide_entries"], landmarks)
            note_resources = _note_resources_by_semantics(model["guide_entries"], landmarks)

            parsed_resources: list[_ParsedResource] = []
            archive_hash = hashlib.sha256()
            with open(path, "rb") as source_file:
                for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
                    archive_hash.update(chunk)
            for resource_index, href in enumerate(model["content_paths"]):
                data = read_member(zf, zf.getinfo(href))
                tree, parse_mode, diagnostics = resource_parser(data)
                parsed_resources.append((resource_index, href, data, tree, parse_mode, diagnostics))
            note_relations = detect_note_relations(
                {href: tree.getroot() for _, href, _, tree, _, _ in parsed_resources},
                excluded_resources=toc_paths,
                note_resources=note_resources,
            )
            protected_paths = {
                href: {
                    tuple(marker["path"])
                    for marker in note_relations["markers"]
                    if marker["resource_href"] == href
                }
                for href in model["content_paths"]
            }
            note_target_paths = {
                href: {
                    tuple(target["path"])
                    for target in note_relations["targets"]
                    if target["resource_href"] == href
                }
                for href in model["content_paths"]
            }

            resources: list[dict[str, object]] = []
            for (
                resource_index,
                href,
                data,
                tree,
                parse_mode,
                diagnostics,
            ) in parsed_resources:
                title, segments, resource = annotate_resource(
                    data,
                    resource_index,
                    href,
                    tree=tree,
                    parse_mode=parse_mode,
                    diagnostics=diagnostics,
                    protected_paths=protected_paths[href],
                    note_target_paths=note_target_paths[href],
                    book_title=book_title,
                    skip_navigation=href in toc_paths,
                )
                resources.append({**resource, "title": title, "segments": segments})
                for segment in segments:
                    existing = segment.meta.get("semantic_hints", [])
                    segment.meta["semantic_hints"] = list(
                        dict.fromkeys([*existing, *resource_hints.get(href, [])])
                    )
            resources_by_href = {resource["href"]: resource for resource in resources}
            spine_resources = [resources_by_href[href] for href in hrefs]
            chapters, split_strategy, split_toc_path = logical_chapters(
                spine_resources, toc_entries
            )
    except ZipSafetyError as exc:
        raise ValueError(f"EPUB archive rejected: {exc.code}") from exc

    return Document(
        title=book_title or os.path.splitext(os.path.basename(path))[0],
        source_lang=source_lang,
        target_lang=target_lang,
        fmt="epub",
        source_path=os.path.abspath(path),
        chapters=chapters,
        meta={
            "epub_schema": 4,
            "epub_sha256": archive_hash.hexdigest(),
            "opf_path": opf_path,
            "toc_paths": toc_paths,
            "toc_entries": toc_entries,
            "guide_entries": model["guide_entries"],
            "nav_landmarks": landmarks,
            "epub_notes": note_relations,
            "epub_note_slots_version": 1,
            "epub_resources": [
                {
                    "index": resource["index"],
                    "href": resource["href"],
                    "resource_sha256": resource["resource_sha256"],
                    "parse_mode": resource["parse_mode"],
                    "parser_diagnostics": resource["parser_diagnostics"],
                }
                for resource in resources
            ],
            "epub_split_strategy": split_strategy,
            "epub_split_toc_path": split_toc_path,
        },
    )


def _source_slots(chapters: Iterable[Chapter]) -> set[tuple[str, tuple[int, ...], str, str]]:
    slots: set[tuple[str, tuple[int, ...], str, str]] = set()
    for chapter in chapters:
        for segment in chapter.segments:
            state = segment.epub_state
            if state is None:
                raise ValueError("EPUB segment is missing slot state; start a new run")
            slots.update(
                (
                    state.resource_href,
                    (*state.block_path, *slot.element_path),
                    slot.field,
                    slot.source_value,
                )
                for slot in state.slots
            )
    return slots


def ensure_slot_compatibility(current: Document, persisted: Iterable[Chapter]) -> None:
    """比较原文槽位覆盖，不受章节分组、重复映射或已保存译文影响。"""
    if _source_slots(current.chapters) != _source_slots(persisted):
        raise ValueError(
            "EPUB slot_layout_mismatch: current extraction rules differ; "
            "preserve this run and start a new run"
        )
