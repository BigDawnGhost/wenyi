"""原文保留范围的语言校验证据。"""

from __future__ import annotations

import os
import zipfile
from collections import defaultdict

from lxml import etree

from trans_novel.assemble.epub.rendering.source_dom import effective_language
from trans_novel.assemble.epub.verification import archive_model, dom
from trans_novel.assemble.epub.verification import bilingual as bilingual_module


def check_root_language(
    root_source, root_output, resource, target_lang, is_ncx, segments, failures, differences
):
    if not target_lang or is_ncx:
        return
    source_attrs = {
        key: value for key, value in root_source.attrib.items() if bilingual_module.lang_attr(key)
    }
    output_attrs = {
        key: value for key, value in root_output.attrib.items() if bilingual_module.lang_attr(key)
    }
    if segments and all(segment.preserve_source for segment in segments):
        if source_attrs != output_attrs:
            failures.append(archive_model.item("dom", "language_mismatch", resource, "source"))
        return
    if set(source_attrs) != set(output_attrs) or any(
        value != target_lang for value in output_attrs.values()
    ):
        failures.append(archive_model.item("dom", "language_mismatch", resource, "target"))
        return
    for key, value in source_attrs.items():
        if output_attrs[key] != value:
            differences["language_fields"] += 1


def preserved_language_paths(root_source, root_output, resource, segments, source_lang, failures):
    paths: set[tuple[int, ...]] = set()
    if segments and all(segment.preserve_source for segment in segments):
        return paths
    by_block: dict[tuple[int, ...], set[bool]] = defaultdict(set)
    for segment in segments:
        state = segment.epub_state
        assert state is not None
        by_block[state.block_path].add(segment.preserve_source)
    for path, decisions in by_block.items():
        if len(decisions) > 1:
            failures.append(
                archive_model.item("state", "preserve_range_ambiguous", resource, "block")
            )
            continue
        if decisions != {True}:
            continue
        source_block = dom.resolve_path_lxml(root_source, path)
        output_block = dom.resolve_path_lxml(root_output, path)
        if source_block is None or output_block is None:
            failures.append(archive_model.item("dom", "block_locator_missing", resource, "output"))
            continue
        source_attrs = {
            key: value
            for key, value in source_block.attrib.items()
            if bilingual_module.lang_attr(key)
        }
        output_attrs = {
            key: value
            for key, value in output_block.attrib.items()
            if bilingual_module.lang_attr(key)
        }
        expected = effective_language(source_block, source_lang or None)
        actual = effective_language(output_block)
        extras = {key: value for key, value in output_attrs.items() if key not in source_attrs}
        existing_changed = any(
            output_attrs.get(key) != value for key, value in source_attrs.items()
        )
        extra_invalid = bool(
            extras
            and (
                set(extras) != {"{http://www.w3.org/XML/1998/namespace}lang"}
                or expected is None
                or next(iter(extras.values())) != expected
            )
        )
        if expected != actual or existing_changed or extra_invalid:
            failures.append(
                archive_model.item("dom", "language_mismatch", resource, "preserved_range")
            )
        if source_attrs != output_attrs:
            paths.add(path)
    return paths


def _generated_navigation_proof(archive, names, chapters, source_lang, failures):
    nav_roots = [
        etree.fromstring(
            archive.read(name),
            etree.XMLParser(no_network=True, resolve_entities=False),
        )
        for name in names
        if name.lower().endswith(("/nav.xhtml", "/nav.html"))
    ]
    for chapter in chapters:
        filename = f"ch{chapter.index}.xhtml"
        labels = [
            node
            for root in nav_roots
            for node in root.iter()
            if isinstance(node.tag, str)
            and node.tag.rsplit("}", 1)[-1].lower() == "a"
            and os.path.basename((node.get("href") or "").split("#", 1)[0]) == filename
        ]
        label = labels[0] if len(labels) == 1 else None
        root = label.getroottree().getroot() if label is not None else None
        lang = next(
            (
                value
                for node in (label, root)
                if node is not None
                for key, value in node.attrib.items()
                if bilingual_module.lang_attr(key)
            ),
            "",
        )
        if (
            label is None
            or "".join(label.itertext()).strip() != chapter.title.strip()
            or (source_lang and lang != source_lang)
        ):
            failures.append(
                archive_model.item("nav", "preserved_generated_label_mismatch", filename, "source")
            )


def _generated_content_proof(archive, names, chapters, source_lang, failures, checked):
    from trans_novel.assemble.epub.rendering.bilingual import BILINGUAL_SOURCE_CLASSES
    from trans_novel.assemble.text import merged_paragraphs
    from trans_novel.ingest import KIND_HEADING

    for chapter in chapters:
        suffix = f"/ch{chapter.index}.xhtml"
        matches = [name for name in names if name.endswith(suffix)]
        if len(matches) != 1:
            failures.append(
                archive_model.item("state", "preserved_chapter_missing", suffix, "source")
            )
            continue
        root = etree.fromstring(
            archive.read(matches[0]),
            etree.XMLParser(no_network=True, resolve_entities=False),
        )
        body = next(
            (
                node
                for node in root.iter()
                if isinstance(node.tag, str)
                and archive_model.local_name(node.tag).lower() == "body"
            ),
            None,
        )
        source_copies = [
            node
            for node in root.iter()
            if isinstance(node.tag, str)
            and BILINGUAL_SOURCE_CLASSES.intersection(node.get("class", "").split())
        ]
        if source_copies:
            failures.append(
                archive_model.item(
                    "bilingual_source",
                    "preserved_generated_duplicate",
                    matches[0],
                    "source",
                )
            )
        actual = [
            (archive_model.local_name(node.tag).lower(), "".join(node.itertext()))
            for node in (body if body is not None else ())
            if isinstance(node.tag, str)
            and archive_model.local_name(node.tag).lower() in {"h1", "p"}
            and not BILINGUAL_SOURCE_CLASSES.intersection(node.get("class", "").split())
        ]
        expected = [
            ("h1" if kind == KIND_HEADING else "p", target)
            for kind, target, _source in merged_paragraphs(chapter)
        ]
        checked["bilingual_source"] += max(len(expected), 1)
        languages = {
            key: value for key, value in root.attrib.items() if bilingual_module.lang_attr(key)
        }
        if actual != expected:
            failures.append(
                archive_model.item("dom", "preserved_generated_text_mismatch", matches[0], "source")
            )
        if source_lang and (
            not languages or any(value != source_lang for value in languages.values())
        ):
            failures.append(archive_model.item("dom", "language_mismatch", matches[0], "source"))


def generated_chapter_proof(output_path, store, failures, checked):
    try:
        manifest = store.load_manifest()
    except Exception:
        return
    declared = any(
        (
            isinstance(processing := meta.get("processing"), dict)
            and processing.get("action") == "preserve"
        )
        or getattr(processing, "action", None) == "preserve"
        for meta in manifest.get("chapters", [])
    )
    if not declared:
        return
    try:
        chapters = [
            chapter
            for meta in manifest["chapters"]
            if (chapter := store.load_chapter(meta["index"])).preserve_source
        ]
    except Exception:
        failures.append(archive_model.item("state", "state_unreadable", "<state>", "source"))
        return
    source_lang = str(manifest.get("source_lang") or "")
    try:
        with zipfile.ZipFile(output_path) as archive:
            names = archive.namelist()
            _generated_navigation_proof(archive, names, chapters, source_lang, failures)
            _generated_content_proof(archive, names, chapters, source_lang, failures, checked)
    except (OSError, zipfile.BadZipFile, etree.XMLSyntaxError):
        failures.append(
            archive_model.item("state", "preserved_chapter_unreadable", "<output>", "source")
        )
