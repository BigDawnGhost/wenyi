"""Plan heading/TOC reuse and commit validated title batches through one service."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from ..agents import prompts
from ..agents.title_translator import TitleOutputError, TitleTranslator
from ..glossary.store import GlossaryStore
from ..ingest.models import Chapter
from ..storage.protocol import Storage


def _flat(value: object) -> str:
    """Normalize title whitespace without changing stable location identity."""
    return " ".join(str(value or "").split())


@dataclass(frozen=True)
class TitleRequest:
    """A pending title and its destination within the plan's owned manifest copy."""

    record: dict[str, Any]
    source: str


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
            item.record["title_translated"] = target or item.source
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


def plan_titles(manifest: dict[str, Any], chapters: Mapping[int, Chapter]) -> TitlePlan:
    """Reuse translated headings and select pending titles without mutating inputs."""
    copied = deepcopy(manifest)
    raw_meta = copied.get("meta")
    meta = raw_meta if isinstance(raw_meta, dict) else {}
    raw_entries = meta.get("toc_entries", [])
    entries = raw_entries if isinstance(raw_entries, list) else []
    entries = [entry for entry in entries if isinstance(entry, dict) and _flat(entry.get("title"))]
    plan = TitlePlan(copied, entries, [])
    headings = _heading_targets(chapters)
    for entry in entries:
        if entry.get("title_translated"):
            continue
        anchor = entry.get("segment_anchor")
        linked = headings.get(anchor) if isinstance(anchor, str) else None
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
        chapter = chapters.get(row.get("index"))
        if chapter is None:
            continue
        heading = next(
            (segment for segment in chapter.text_segments if segment.kind == "heading"), None
        )
        if heading and heading.anchor and _flat(heading.source) == _flat(row.get("title")):
            target = headings.get(heading.anchor, ("", ""))[1]
            if target.strip():
                row["title_translated"] = target.strip()
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


class TitleTranslationService:
    """Own title planning, manifest commits and title events under the caller's book lock."""

    def __init__(self, translator: TitleTranslator) -> None:
        self._translator = translator

    def run(
        self,
        store: Storage,
        glossary: Storage | GlossaryStore,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> None:
        """Reuse complete headings, then translate and commit only pending title batches."""
        manifest = store.load_manifest()
        chapters = {
            row["index"]: store.load_chapter(row["index"])
            for row in manifest.get("chapters", [])
            if isinstance(row.get("index"), int)
        }
        plan = plan_titles(manifest, chapters)
        if plan.changed:
            store.save_manifest(plan.manifest)
        if not plan.pending:
            store.log_event("titles_skipped", reason="already_translated_or_reused")
            return
        if progress:
            progress(0, len(plan.pending), "Translating chapter titles…")

        batches = title_batches(plan.pending)
        completed = 0
        glossary_text = prompts.render_glossary(glossary.all_terms())
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
                progress(completed, len(plan.pending), "Translating chapter titles")
