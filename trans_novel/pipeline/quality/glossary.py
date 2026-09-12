"""RunStore/chapter orchestration for glossary auditing."""

from __future__ import annotations

import re
from typing import Any

from trans_novel.epub.slots import distribute_slot_translation, target_slot_text
from trans_novel.glossary.audit import (
    CJK_SPACE_GAP_RE,
    apply_unifications,
    find_candidates,
    has_cjk,
    is_latin_source,
)
from trans_novel.glossary.store import GlossaryStore


def target_corpus(store) -> str:
    manifest = store.load_manifest()
    parts: list[str] = []
    for chapter in manifest["chapters"]:
        loaded = store.load_chapter(chapter["index"])
        if not loaded.preserve_source:
            parts.extend(segment.target or "" for segment in loaded.text_segments)
    return "\n".join(parts)


def _preserved_manifest_chapter(chapter: dict[str, Any]) -> bool:
    processing = chapter.get("processing")
    if isinstance(processing, dict):
        return processing.get("action") == "preserve"
    return getattr(processing, "action", None) == "preserve"


def rewrite_targets(store, glossary: GlossaryStore, replace_map: dict[str, str]) -> int:
    """把各章 target 里的变体替换为规范译法。返回改动段数。"""
    variants_sorted = sorted(replace_map, key=len, reverse=True)

    def apply(text: str, executed: list[dict[str, str]]) -> str:
        if not text:
            return text
        for variant in variants_sorted:
            if variant in text:
                text = text.replace(variant, replace_map[variant])
                executed.append({"variant": variant, "canonical": replace_map[variant]})
        return text

    manifest = store.load_manifest()
    changed = 0
    for chapter in manifest["chapters"]:
        loaded = store.load_chapter(chapter["index"])
        if loaded.preserve_source:
            continue
        dirty = False
        entries: list[dict[str, Any]] = []
        for index, segment in enumerate(loaded.segments):
            if segment.target is None:
                continue
            executed: list[dict[str, str]] = []
            if segment.epub_state is not None:
                old = target_slot_text(segment.epub_state.slots)
                new = apply(old, executed)
                if new == old:
                    continue
                segment.assign_translation(distribute_slot_translation(segment.epub_state, new))
            else:
                old = segment.target
                new = apply(old, executed)
                if new == old:
                    continue
                segment.assign_translation(new)
            dirty = True
            changed += 1
            entries.append(
                {
                    "chapter": chapter["index"],
                    "index": index,
                    "before": old,
                    "after": segment.target,
                    "replacements": executed,
                }
            )
        if dirty:
            store.save_chapter(loaded)
            for entry in entries:
                store.log_event("glossary_rewrite_applied", **entry)

    manifest_dirty = False
    if "title_translated" in manifest:
        old_title = manifest.pop("title_translated")
        manifest_dirty = True
        store.log_event(
            "glossary_book_title_translation_removed",
            title=True,
            before=old_title,
            replace_map=replace_map,
        )
    for chapter in manifest["chapters"]:
        if _preserved_manifest_chapter(chapter):
            continue
        old_title = chapter.get("title_translated")
        new_title = apply(old_title, []) if isinstance(old_title, str) else old_title
        if new_title != old_title:
            chapter["title_translated"] = new_title
            manifest_dirty = True
            store.log_event(
                "glossary_title_rewrite_applied",
                chapter=chapter["index"],
                before=old_title,
                after=new_title,
                replace_map=replace_map,
            )
    if manifest_dirty:
        store.save_manifest(manifest)
    return changed


def fix_latin_residue(store, glossary: GlossaryStore) -> list[dict[str, Any]]:
    """确定性修复锁定术语的拉丁 source 残留。"""
    terms = [term for term in glossary.all_terms() if term.locked and is_latin_source(term.source)]
    if not terms:
        return []
    compiled = [(term, re.compile(r"\b" + re.escape(term.source) + r"\b")) for term in terms]
    applied: list[dict[str, Any]] = []
    touched_sources: set[str] = set()
    manifest = store.load_manifest()
    for chapter in manifest["chapters"]:
        loaded = store.load_chapter(chapter["index"])
        if loaded.preserve_source:
            continue
        dirty = False
        entries: list[dict[str, Any]] = []
        for index, segment in enumerate(loaded.segments):
            for term, pattern in compiled:
                if segment.epub_state is not None:
                    full_target = target_slot_text(segment.epub_state.slots)
                    if not has_cjk(full_target) or term.target in full_target:
                        continue
                    matches = list(pattern.finditer(full_target))
                    if not matches:
                        continue
                    pieces: list[str] = []
                    last = 0
                    replaced_any = False
                    for match in matches:
                        left = full_target[max(0, match.start() - 12) : match.start()]
                        right = full_target[match.end() : match.end() + 12]
                        if not (has_cjk(left) or has_cjk(right)):
                            continue
                        pieces.extend((full_target[last : match.start()], term.target))
                        last = match.end()
                        replaced_any = True
                    if not replaced_any:
                        continue
                    pieces.append(full_target[last:])
                    new = CJK_SPACE_GAP_RE.sub("", "".join(pieces))
                    old = segment.target
                    segment.assign_translation(distribute_slot_translation(segment.epub_state, new))
                    dirty = True
                    touched_sources.add(term.source)
                    entries.append(
                        {
                            "chapter": chapter["index"],
                            "index": index,
                            "before": old,
                            "after": segment.target,
                            "term_source": term.source,
                            "term_target": term.target,
                        }
                    )
                    continue
                text = segment.target
                if not text or not has_cjk(text) or term.target in text:
                    continue
                matches = list(pattern.finditer(text))
                if not matches:
                    continue
                pieces = []
                last = 0
                replaced = False
                for match in matches:
                    left = text[max(0, match.start() - 12) : match.start()]
                    right = text[match.end() : match.end() + 12]
                    if not (has_cjk(left) or has_cjk(right)):
                        continue
                    pieces.append(text[last : match.start()])
                    pieces.append(term.target)
                    last = match.end()
                    replaced = True
                if not replaced:
                    continue
                pieces.append(text[last:])
                new = CJK_SPACE_GAP_RE.sub("", "".join(pieces))
                old = text
                segment.assign_translation(new)
                dirty = True
                touched_sources.add(term.source)
                entries.append(
                    {
                        "chapter": chapter["index"],
                        "index": index,
                        "before": old,
                        "after": new,
                        "term_source": term.source,
                        "term_target": term.target,
                    }
                )
        if dirty:
            store.save_chapter(loaded)
            for entry in entries:
                store.log_event("glossary_latin_residue_fixed", **entry)
    for term in terms:
        if term.source in touched_sources:
            applied.append(
                {
                    "source": term.source,
                    "canonical": term.target,
                    "variants": [term.source],
                    "reason": "锁定术语拉丁残留替换",
                }
            )
    return applied


def audit_glossary(store, glossary: GlossaryStore, auditor) -> list[dict[str, Any]]:
    candidates = find_candidates(
        glossary.all_terms(), glossary.open_conflicts(), target_corpus(store)
    )
    unifications = auditor.decide(candidates)
    applied, replace_map = apply_unifications(glossary, unifications)
    if replace_map:
        rewrite_targets(store, glossary, replace_map)
    applied.extend(fix_latin_residue(store, glossary))
    return applied
