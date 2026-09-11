"""Read EPUB sources into schema-4 structural text-slot state."""

from __future__ import annotations

import hashlib
import os
import zipfile
from collections.abc import Iterable
from typing import Any

from trans_novel.epub.archive import ZipSafetyError, preflight_zip, read_member
from trans_novel.epub.navigation import parse_toc_entries
from trans_novel.epub.package import HTML_MEDIA, read_package
from trans_novel.ingest.epub.chapters import logical_chapters
from trans_novel.ingest.epub.markup import annotate_resource
from trans_novel.ingest.models import Chapter, Document


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

            resources: list[dict[str, object]] = []
            archive_hash = hashlib.sha256()
            with open(path, "rb") as source_file:
                for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
                    archive_hash.update(chunk)
            for resource_index, href in enumerate(model["content_paths"]):
                data = read_member(zf, zf.getinfo(href))
                title, segments, resource = annotate_resource(
                    data,
                    resource_index,
                    href,
                    book_title=book_title,
                    skip_navigation=href in toc_paths,
                )
                resources.append({**resource, "title": title, "segments": segments})
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
