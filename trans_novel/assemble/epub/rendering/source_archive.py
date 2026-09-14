"""Render translated schema-4 EPUBs while preserving source archive members."""

from __future__ import annotations

import hashlib
import zipfile
from copy import copy
from typing import cast

from lxml import etree

from trans_novel.assemble.epub.metadata import epub_language
from trans_novel.assemble.epub.rendering.bilingual import dedupe_segment_mappings
from trans_novel.assemble.epub.rendering.source_dom import toc_kind_at
from trans_novel.assemble.epub.rendering.source_markup import (
    parse_source_markup,
    render_source_resource,
    rewrite_markup_languages,
    rewrite_toc_lxml,
    serialize_source_tree,
)
from trans_novel.assemble.epub.rendering.theme import ResourceThemeScope, ThemePlan
from trans_novel.assemble.epub.rendering.theme.service import ThemeService
from trans_novel.epub.archive import MetadataZipFile, ZipSafetyError, preflight_zip, read_member
from trans_novel.epub.notes import NoteRelations
from trans_novel.epub.slots import slot_contract_digest
from trans_novel.ingest import Segment, preserved_toc_entry_ids
from trans_novel.ingest.epub.reader import ensure_slot_compatibility, read_epub


def _write_source_member(
    zout: MetadataZipFile,
    info: zipfile.ZipInfo,
    data: bytes,
    *,
    compress_type: int | None = None,
) -> None:
    preserved = copy(info)
    if compress_type is not None:
        preserved.compress_type = compress_type
    zout.writestr(preserved, data)


def _rewrite_opf_language_lxml(tree: etree._ElementTree, target_lang: str) -> None:
    dc_language = "{http://purl.org/dc/elements/1.1/}language"
    for node in tree.getroot().iter():
        if node.tag == dc_language:
            node.text = target_lang
            break


def _archive_digest(source_path: str) -> str:
    digest = hashlib.sha256()
    with open(source_path, "rb") as source_file:
        for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_state(
    store, source_path
) -> tuple[
    dict[str, object],
    str,
    dict[str, dict[str, object]],
    list[Segment],
    list[dict[str, object]],
    NoteRelations,
    str,
]:
    manifest = store.load_manifest()
    raw_meta = manifest.get("meta")
    raw_source_lang = manifest.get("source_lang", "")
    source_lang = raw_source_lang if isinstance(raw_source_lang, str) else ""
    meta = raw_meta if isinstance(raw_meta, dict) else {}
    schema = meta.get("epub_schema")
    if schema != 4:
        raise ValueError(
            f"Unsupported EPUB state schema {schema!r}; start a fresh translation for schema 4"
        )
    if meta.get("epub_sha256") != _archive_digest(source_path):
        raise ValueError("EPUB source archive digest mismatch")
    resources_meta = {
        str(item.get("href")): item
        for item in meta.get("epub_resources", [])
        if isinstance(item, dict) and isinstance(item.get("href"), str)
    }
    chapters = [store.load_chapter(c["index"]) for c in manifest["chapters"]]
    all_segments = [segment for chapter in chapters for segment in chapter.segments]
    deduped_segments = dedupe_segment_mappings(all_segments)
    grouped: dict[str, list[Segment]] = {}
    for segment in deduped_segments:
        if segment.epub_state is None or not segment.resource_href:
            raise ValueError("EPUB state contains a segment without schema-4 slot metadata")
        state = segment.epub_state
        if state.slot_contract_sha256 != slot_contract_digest(state.slots):
            raise ValueError(f"EPUB slot contract digest mismatch: {segment.resource_href}")
        grouped.setdefault(segment.resource_href, []).append(segment)
    current = read_epub(source_path, source_lang, manifest.get("target_lang", "zh"))
    ensure_slot_compatibility(current, chapters)
    raw_toc = meta.get("toc_entries", [])
    toc_source = raw_toc if isinstance(raw_toc, list) else []
    preserved_toc_ids = preserved_toc_entry_ids(chapters, toc_source)
    toc_entries = [
        {**entry, "preserve_source": True}
        if entry.get("id") in preserved_toc_ids or entry.get("entry_id") in preserved_toc_ids
        else entry
        for entry in toc_source
        if isinstance(entry, dict)
    ]
    return (
        meta,
        source_lang,
        resources_meta,
        deduped_segments,
        toc_entries,
        cast(NoteRelations, current.meta["epub_notes"]),
        str(current.meta["epub_sha256"]),
    )


def _resource_note_paths(relations: NoteRelations, resource: str) -> tuple[tuple[int, ...], ...]:
    return tuple(
        tuple(item["path"])
        for item in (*relations["markers"], *relations["targets"])
        if item["resource_href"] == resource
    )


def _render_source_archive(
    source_path: str,
    out_path: str,
    *,
    meta: dict[str, object],
    source_lang: str,
    resources_meta: dict[str, dict[str, object]],
    grouped: dict[str, list[Segment]],
    toc_entries: list[dict[str, object]],
    target_lang: str,
    bilingual: bool,
    order: str,
    note_relations: NoteRelations,
    source_sha256: str,
    theme: ThemeService | None = None,
) -> ThemePlan | None:
    archive_lang = (
        source_lang
        if grouped
        and all(segment.preserve_source for segments in grouped.values() for segment in segments)
        and source_lang
        else target_lang
    )
    scopes: dict[str, ResourceThemeScope] | None = {} if theme is not None else None
    with zipfile.ZipFile(source_path, "r") as zin:
        try:
            preflight_zip(zin)
        except ZipSafetyError as exc:
            raise ValueError(f"EPUB archive rejected: {exc.code}") from exc
        with MetadataZipFile(out_path, "w") as zout:
            zout.comment = zin.comment
            opf_path = str(meta.get("opf_path") or "")
            mimetype_info = next(
                (info for info in zin.infolist() if info.filename == "mimetype"), None
            )
            if mimetype_info is None:
                raise ValueError("EPUB archive missing mimetype")
            _write_source_member(
                zout, mimetype_info, b"application/epub+zip", compress_type=zipfile.ZIP_STORED
            )
            for info in zin.infolist():
                name = info.filename
                if name == "mimetype":
                    continue
                data = read_member(zin, info)
                resource_info = resources_meta.get(name)
                if resource_info is not None and hashlib.sha256(data).hexdigest() != str(
                    resource_info.get("resource_sha256", "")
                ):
                    raise ValueError(f"EPUB resource digest mismatch: {name}")
                if name == opf_path:
                    tree, mode = parse_source_markup(data)
                    _rewrite_opf_language_lxml(tree, archive_lang)
                    _write_source_member(zout, info, serialize_source_tree(tree, data, mode))
                elif name in grouped:
                    resource = resources_meta.get(name)
                    if resource is None:
                        raise ValueError(f"EPUB resource missing persisted metadata: {name}")
                    rendered = render_source_resource(
                        data,
                        name,
                        grouped[name],
                        expected_digest=str(resource.get("resource_sha256", "")),
                        expected_mode=str(resource.get("parse_mode", "")),
                        target_lang=target_lang,
                        bilingual=bilingual,
                        order=order,
                        source_lang=source_lang,
                        scope_sink=scopes,
                        note_source_paths=_resource_note_paths(note_relations, name),
                    )
                    toc_kind = toc_kind_at(toc_entries, name)
                    if toc_kind in {"nav", "ncx"}:
                        rendered = rewrite_toc_lxml(
                            rendered,
                            toc_entries,
                            is_ncx=toc_kind == "ncx",
                            toc_path=name,
                            target_lang=archive_lang,
                            source_lang=source_lang,
                        )
                    _write_source_member(zout, info, rendered)
                elif toc_kind_at(toc_entries, name) in {"nav", "ncx"}:
                    resource = resources_meta.get(name)
                    expected_mode = str(resource.get("parse_mode", "")) if resource else None
                    kind = toc_kind_at(toc_entries, name)
                    _write_source_member(
                        zout,
                        info,
                        rewrite_toc_lxml(
                            data,
                            toc_entries,
                            is_ncx=kind == "ncx",
                            toc_path=name,
                            target_lang=archive_lang,
                            expected_mode=expected_mode,
                            source_lang=source_lang,
                        ),
                    )
                elif name in resources_meta:
                    resource = resources_meta[name]
                    tree, mode = parse_source_markup(data, str(resource.get("parse_mode", "")))
                    rewrite_markup_languages(tree.getroot(), archive_lang)
                    _write_source_member(zout, info, serialize_source_tree(tree, data, mode))
                else:
                    _write_source_member(zout, info, data)
    return (
        theme.render(
            out_path,
            scopes,
            bilingual=bilingual,
            note_relations=note_relations,
            source_sha256=source_sha256,
            target_lang=target_lang,
        )
        if theme is not None and scopes is not None
        else None
    )


def assemble_source_epub(
    store,
    source_path: str,
    out_path: str,
    *,
    target_lang: str,
    bilingual: bool = False,
    order: str = "target_first",
    theme: ThemeService | None = None,
) -> ThemePlan | None:
    if order not in {"target_first", "source_first"}:
        raise ValueError(f"invalid bilingual order: {order!r}")
    (
        meta,
        source_lang,
        resources_meta,
        deduped_segments,
        toc_entries,
        note_relations,
        source_sha256,
    ) = _source_state(store, source_path)
    grouped: dict[str, list[Segment]] = {}
    for segment in deduped_segments:
        assert segment.epub_state is not None
        grouped.setdefault(segment.resource_href, []).append(segment)
    return _render_source_archive(
        source_path,
        out_path,
        meta=meta,
        source_lang=source_lang,
        resources_meta=resources_meta,
        grouped=grouped,
        toc_entries=toc_entries,
        target_lang=target_lang,
        bilingual=bilingual,
        order=order,
        theme=theme,
        note_relations=note_relations,
        source_sha256=source_sha256,
    )


def assemble_epub(
    store,
    source_path: str,
    out_path: str,
    *,
    bilingual: bool = False,
    order: str = "target_first",
    theme: ThemeService | None = None,
) -> ThemePlan | None:
    manifest = store.load_manifest()
    meta = manifest.get("meta") if isinstance(manifest.get("meta"), dict) else {}
    schema = meta.get("epub_schema")
    if schema != 4:
        raise ValueError(
            f"Unsupported EPUB state schema {schema!r}; start a fresh translation for schema 4"
        )
    return assemble_source_epub(
        store,
        source_path,
        out_path,
        target_lang=epub_language(manifest.get("target_lang", "zh")),
        bilingual=bilingual,
        order=order,
        theme=theme,
    )
