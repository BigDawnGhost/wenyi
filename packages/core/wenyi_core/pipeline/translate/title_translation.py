"""Plan heading/TOC reuse and commit validated title batches through one service."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from functools import cache
from typing import Any

from ...agents import prompts
from ...agents.title_translator import TitleOutputError, TitleTranslator
from ...events import ProgressFn, emit_progress
from ...glossary.store import GlossaryStore
from ...models.document import Chapter, uses_epub_template
from ...models.glossary import GlossaryTerm
from ...storage.protocol import Storage


def _flat(value: object) -> str:
    """Normalize title whitespace without changing stable location identity."""
    return " ".join(str(value or "").split())


@dataclass(frozen=True)
class TitleRequest:
    """A pending title and its destination within the plan's owned manifest copy."""

    record: dict[str, Any]
    source: str
    field: str = "title_translated"
    list_index: int | None = None
    list_size: int = 0


@dataclass
class TitlePlan:
    """Own title mutations until the caller commits a complete manifest snapshot."""

    manifest: dict[str, Any]
    entries: list[dict[str, Any]]
    pending: list[TitleRequest]
    changed: bool = False

    def sync_chapter_titles(self) -> None:
        """Reuse the starting TOC node's translation for its logical chapter."""
        entry_by_id = {
            entry["entry_id"]: entry
            for entry in self.entries
            if isinstance(entry.get("entry_id"), str)
        }
        for chapter in self.manifest.get("chapters", []):
            if chapter.get("title_translated"):
                continue
            entry = entry_by_id.get(chapter.get("toc_entry_id"))
            translated = entry.get("title_translated") if entry is not None else None
            if isinstance(translated, str) and translated.strip():
                chapter["title_translated"] = translated.strip()
                self.changed = True

    def apply(self, batch: list[TitleRequest], translated: list[str]) -> None:
        """Apply a validated batch, including source fallback for empty titles."""
        for item, target in zip(batch, translated):
            value = target or item.source
            if item.list_index is not None:
                bucket = item.record.get(item.field)
                if not isinstance(bucket, list) or len(bucket) != item.list_size:
                    bucket = [""] * item.list_size
                    item.record[item.field] = bucket
                bucket[item.list_index] = value
            else:
                item.record[item.field] = value
        self.sync_chapter_titles()


def _heading_targets(chapters: Mapping[int, Chapter]) -> dict[str, tuple[str, str]]:
    """Index complete heading translations, merging continuation slices by anchor."""
    targets: dict[str, tuple[str, str]] = {}

    def flush(
        anchor: str | None, kind: str, complete: bool, sources: list[str], translations: list[str]
    ) -> None:
        if anchor and kind == "heading" and complete and translations:
            targets[anchor] = ("".join(sources), "".join(translations))

    for chapter in chapters.values():
        anchor: str | None = None
        kind = ""
        translations: list[str] = []
        sources: list[str] = []
        complete = True
        for segment in chapter.text_segments:
            if segment.anchor:
                flush(anchor, kind, complete, sources, translations)
                anchor = segment.anchor
                kind = segment.kind
                translations = [segment.target] if segment.target else []
                sources = [segment.source]
                complete = bool(segment.target and segment.target.strip())
            elif segment.cont and anchor:
                sources.append(segment.source)
                if segment.target and segment.target.strip():
                    translations.append(segment.target)
                else:
                    complete = False
            else:
                flush(anchor, kind, complete, sources, translations)
                anchor = None
                kind = ""
                translations = []
                sources = []
                complete = True
        flush(anchor, kind, complete, sources, translations)
    return targets


def plan_titles(
    manifest: dict[str, Any],
    chapters: Mapping[int, Chapter] | Callable[[], Mapping[int, Chapter]],
) -> TitlePlan:
    """Plan titles without mutating inputs, loading chapters only for heading candidates."""
    copied = deepcopy(manifest)
    raw_meta = copied.get("meta")
    meta = raw_meta if isinstance(raw_meta, dict) else {}
    raw_entries = meta.get("toc_entries", [])
    entries = raw_entries if isinstance(raw_entries, list) else []
    entries = [entry for entry in entries if isinstance(entry, dict) and _flat(entry.get("title"))]
    plan = TitlePlan(copied, entries, [])

    @cache
    def heading_data() -> tuple[Mapping[int, Chapter], dict[str, tuple[str, str]]]:
        loaded = chapters if isinstance(chapters, Mapping) else chapters()
        return loaded, _heading_targets(loaded)

    for entry in entries:
        if entry.get("title_translated"):
            continue
        anchor = entry.get("segment_anchor")
        if not isinstance(anchor, str) or not anchor:
            continue
        _, headings = heading_data()
        linked = headings.get(anchor)
        can_reuse = linked is not None and _flat(linked[0]) == _flat(entry.get("title"))
        target = linked[1] if linked and can_reuse else ""
        if target.strip():
            entry["title_translated"] = target.strip()
            plan.changed = True
    plan.sync_chapter_titles()

    # Spine fallback chapters may reuse their first heading without a TOC entry.
    for row in copied.get("chapters", []):
        if row.get("title_translated"):
            continue
        title = _flat(row.get("title"))
        if not title:
            continue
        loaded, headings = heading_data()
        chapter = loaded.get(row.get("index"))
        if chapter is None:
            continue
        heading = next(
            (segment for segment in chapter.text_segments if segment.kind == "heading"), None
        )
        if heading and heading.anchor and _flat(heading.source) == title:
            linked = headings.get(heading.anchor)
            if linked and linked[1].strip():
                row["title_translated"] = linked[1].strip()
                plan.changed = True

    plan.pending = [
        TitleRequest(entry, _flat(entry.get("title")))
        for entry in entries
        if not entry.get("title_translated")
    ]
    plan.pending.extend(
        TitleRequest(row, _flat(row.get("title")))
        for row in copied.get("chapters", [])
        if _flat(row.get("title"))
        and not row.get("title_translated")
        and not row.get("toc_entry_id")
    )

    if (
        copied.get("fmt") == "epub"
        and isinstance(copied.get("meta"), dict)
        and copied["meta"].get("opf_path")
    ):
        epub_meta = copied["meta"]
        book_title = _flat(copied.get("title"))
        if book_title and not _flat(epub_meta.get("book_title_translated")):
            # Reuse chapter 0's translated title when it already matches the book title.
            first_chapter = copied.get("chapters", [None])[0] if copied.get("chapters") else None
            if (
                isinstance(first_chapter, dict)
                and _flat(first_chapter.get("title")) == book_title
                and _flat(first_chapter.get("title_translated"))
            ):
                epub_meta["book_title_translated"] = _flat(first_chapter["title_translated"])
                plan.changed = True
            else:
                plan.pending.append(
                    TitleRequest(epub_meta, book_title, field="book_title_translated")
                )
        raw_subjects = epub_meta.get("epub_subjects")
        if isinstance(raw_subjects, list) and raw_subjects:
            subjects = [
                _flat(item) for item in raw_subjects if isinstance(item, str) and _flat(item)
            ]
            existing_subjects = epub_meta.get("epub_subjects_translated")
            aligned_existing = (
                existing_subjects
                if isinstance(existing_subjects, list) and len(existing_subjects) == len(subjects)
                else None
            )
            if subjects and (
                aligned_existing is None
                or any(not isinstance(item, str) or not item.strip() for item in aligned_existing)
            ):
                for idx, subject in enumerate(subjects):
                    if (
                        aligned_existing is not None
                        and isinstance(aligned_existing[idx], str)
                        and aligned_existing[idx].strip()
                    ):
                        continue
                    plan.pending.append(
                        TitleRequest(
                            epub_meta,
                            subject,
                            field="epub_subjects_translated",
                            list_index=idx,
                            list_size=len(subjects),
                        )
                    )
    return plan


def title_batches(pending: list[TitleRequest]) -> list[list[TitleRequest]]:
    """Keep the established 40-title and 4,000-character request boundaries."""
    batches: list[list[TitleRequest]] = []
    current: list[TitleRequest] = []
    current_chars = 0
    for item in pending:
        if current and (len(current) >= 40 or current_chars + len(item.source) > 4000):
            batches.append(current)
            current = []
            current_chars = 0
        current.append(item)
        current_chars += len(item.source)
    if current:
        batches.append(current)
    return batches


def _store_navigation_terms(plan: TitlePlan, glossary: Storage | GlossaryStore) -> None:
    """Publish all saved EPUB navigation pairs, including reused and resumed titles."""
    if not plan.entries or not uses_epub_template(
        plan.manifest.get("fmt"), plan.manifest.get("meta")
    ):
        return
    targets = {term.source: term.target for term in glossary.all_terms()}
    conflicts = {
        (row["source"], row["existing_target"], row["proposed_target"])
        for row in glossary.open_conflicts()
    }
    for entry in plan.entries:
        source = _flat(entry.get("title"))
        translated = entry.get("title_translated")
        if not isinstance(translated, str) or not translated.strip():
            continue
        target = translated.strip()
        existing = targets.get(source)
        if existing == target or (source, existing, target) in conflicts:
            continue
        result = glossary.upsert_term(GlossaryTerm(source=source, target=target))
        if result == "conflict":
            conflicts.add((source, existing, target))
        else:
            targets[source] = target


class TitleTranslationService:
    """Own title planning, manifest commits and title events under the caller's book lock."""

    def __init__(self, translator: TitleTranslator) -> None:
        self._translator = translator

    def run(
        self,
        store: Storage,
        glossary: Storage | GlossaryStore,
        progress: ProgressFn | None = None,
    ) -> None:
        """Reuse complete headings, then translate and commit only pending title batches."""
        manifest = store.load_manifest()

        def load_chapters() -> dict[int, Chapter]:
            return {
                row["index"]: store.load_chapter(row["index"])
                for row in manifest.get("chapters", [])
                if isinstance(row.get("index"), int)
            }

        plan = plan_titles(manifest, load_chapters)
        if plan.changed:
            store.save_manifest(plan.manifest)
        if not plan.pending:
            _store_navigation_terms(plan, glossary)
            store.log_event("titles_skipped", reason="already_translated_or_reused")
            return
        if progress:
            progress(0, len(plan.pending), "Translating chapter titles…")

        batches = title_batches(plan.pending)
        completed = 0
        glossary_text = prompts.render_glossary(
            glossary.all_terms(), source_lang=self._translator.src
        )
        for batch_index, batch in enumerate(batches):
            titles = [item.source for item in batch]
            try:
                translated = self._translator.translate(titles, glossary_text)
            except TitleOutputError as error:
                store.log_event(
                    "titles_translation_rejected",
                    batch=batch_index,
                    reason="count_mismatch",
                    expected=error.expected,
                    actual=error.actual,
                )
                raise
            except Exception as error:
                store.log_event(
                    "titles_translation_failed",
                    batch=batch_index,
                    count=len(titles),
                    error=repr(error),
                )
                raise
            plan.apply(batch, translated)
            store.save_manifest(plan.manifest)
            store.log_event(
                "titles_translated",
                batch=batch_index,
                titles=[
                    {"source": source, "target": target}
                    for source, target in zip(titles, translated)
                ],
            )
            completed += len(batch)
            if progress:
                emit_progress(
                    progress,
                    completed,
                    len(plan.pending),
                    "Translating chapter titles",
                    payload={"batch": batch_index, "batch_size": len(batch)},
                )
        # Commit titles first so an interrupted glossary write can resume without model calls.
        _store_navigation_terms(plan, glossary)
