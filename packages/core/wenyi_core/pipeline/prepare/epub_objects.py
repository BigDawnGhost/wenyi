"""Refresh protected-object references on resume without changing saved translation text."""

from __future__ import annotations

from copy import deepcopy

from ...ingest.epub_reader import read_epub
from ...markup.contracts import ANNOTATION_META_KEY
from ...markup.objects import OBJECT_RELATION, OBJECT_SCHEMA_VERSION
from ...models.document import Document, Segment
from ...storage.protocol import Storage


def refresh_epub_objects(
    store: Storage, source_path: str, *, document: Document | None = None
) -> None:
    """Commit reparsed metadata last, after validating every original logical source.

    The caller holds the book lock and has checked source_sha256. Interrupted refreshes
    can repeat safely; targets, continuations, glossary checkpoints and chapter status stay intact.
    """
    manifest = store.load_manifest()
    meta = manifest.get("meta", {})
    if manifest.get("fmt") != "epub" or meta.get("epub_objects") == OBJECT_SCHEMA_VERSION:
        return
    document = document or read_epub(source_path, manifest["source_lang"], manifest["target_lang"])
    fresh: dict[tuple[str | None, str], Segment] = {}
    for chapter in document.chapters:
        first: Segment | None = None
        for segment in chapter.segments:
            if segment.cont and first is not None:
                first.source += segment.source
            elif segment.anchor:
                first = segment.model_copy(deep=True)
                fresh[(segment.resource_href, segment.anchor)] = first
    store.ensure_source_identity(source_path)
    with store.state_lock():
        manifest = store.load_manifest()
        updates = []
        for entry in manifest["chapters"]:
            chapter = store.load_chapter(entry["index"])
            segments = chapter.text_segments
            position = 0
            changed = False
            while position < len(segments):
                first = segments[position]
                end = position + 1
                while end < len(segments) and segments[end].cont:
                    end += 1
                parsed = fresh.get((first.resource_href, first.anchor)) if first.anchor else None
                source = "".join(segment.source for segment in segments[position:end])
                if parsed is None or parsed.source != source:
                    raise ValueError(
                        "EPUB source does not match saved paragraphs; "
                        "cannot refresh protected-object references"
                    )
                annotations = parsed.meta.get(ANNOTATION_META_KEY, {})
                if any(
                    item.get("relation") == OBJECT_RELATION for item in annotations.get("items", [])
                ):
                    previous = first.meta.get(ANNOTATION_META_KEY, {})
                    first.meta[ANNOTATION_META_KEY] = deepcopy(annotations)
                    for key in ("placements", "target_digest"):
                        if key in previous:
                            first.meta[ANNOTATION_META_KEY][key] = deepcopy(previous[key])
                    changed = True
                position = end
            if changed:
                updates.append(chapter)
        registry = store.load_annotation_contexts() or {"version": 1, "contexts": {}}
        registry.setdefault("contexts", {}).update(
            document.meta.get("epub_annotation_contexts", {}).get("contexts", {})
        )
        for chapter in updates:
            store.save_chapter(chapter)
        store.save_annotation_contexts(registry)
        manifest.setdefault("meta", {})["epub_objects"] = OBJECT_SCHEMA_VERSION
        store.save_manifest(manifest)
