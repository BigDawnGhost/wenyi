"""Resolve protected-object markers on disposable export copies."""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterable, Iterator
from copy import deepcopy
from typing import Any, Protocol, TypeAlias

from ..alignment.objects import resolve_objects
from ..i18n.policy.models import PolicyContext, PolicyPlan
from ..i18n.policy.resolver import resolve_policy
from ..markup.contexts import annotation_metadata
from ..markup.objects import export_object_placements, object_anchors
from ..models.document import Chapter
from ..storage.protocol import Storage
from ..storage.run_store import ExportSnapshotStore, RunStore
from .quotes import QuoteStyle, normalize_quotes
from .writer_common import _manifest_target_lang


class ExportChapterSource(Protocol):
    """Storage subset the export view needs: manifest and chapter reads plus manifest saves."""

    @property
    def run_dir(self) -> str: ...

    def load_manifest(self) -> dict: ...

    def load_chapter(self, ci: int) -> Chapter: ...

    def save_manifest(self, manifest: dict) -> None: ...


class ExportViewStore(RunStore):
    """Overlay read-only export transformations on RunStore and delegate other capabilities."""

    def __init__(
        self,
        store: ExportChapterSource,
        *,
        plan: PolicyPlan | None = None,
        quote_style: QuoteStyle = "preserve",
    ) -> None:
        super().__init__(store.run_dir, create=False)
        self._store = store
        self._chapters: dict[int, Chapter] = {}
        self._quote_style: QuoteStyle = quote_style
        self._target_lang = ""
        self._manifest: dict | None = None
        self._manifest_lock = threading.Lock()
        manifest = self.load_manifest()
        self.language_policy = plan or resolve_policy(
            PolicyContext(
                manifest.get("source_lang", "auto"),
                self._target_lang,
                phase="export",
                format="epub",
                source_identity=manifest.get("source_sha256", ""),
            )
        )

    def load_manifest(self) -> dict:
        """Return the export-session manifest, querying storage once per view.

        Translated titles and metadata carry the export-only quote normalization.
        """
        if self._manifest is None:
            with self._manifest_lock:
                if self._manifest is None:
                    manifest = self._store.load_manifest()
                    self._target_lang = _manifest_target_lang(manifest)
                    if self._quote_style != "preserve":
                        manifest = deepcopy(manifest)
                        self._normalize_manifest_quotes(manifest)
                    self._manifest = manifest
        return deepcopy(self._manifest)

    def save_manifest(self, manifest: dict) -> None:
        """Delegate the save and drop this session's stale copy."""
        self._store.save_manifest(manifest)
        with self._manifest_lock:
            self._manifest = None

    def _normalize_manifest_quotes(self, manifest: dict) -> None:
        """Normalize quotes in translated manifest titles and metadata in place."""
        for row in manifest.get("chapters", []):
            if isinstance(row.get("title_translated"), str):
                row["title_translated"] = normalize_quotes(
                    row["title_translated"], self._quote_style, target_lang=self._target_lang
                )
        meta = manifest.get("meta")
        if not isinstance(meta, dict):
            return
        for key in ("book_title_translated", "epub_description_translated"):
            if isinstance(meta.get(key), str):
                meta[key] = normalize_quotes(
                    meta[key], self._quote_style, target_lang=self._target_lang
                )
        for key in (
            "epub_subjects_translated",
            "epub_creators_translated",
            "epub_nav_titles_translated",
        ):
            if isinstance(meta.get(key), list):
                meta[key] = [
                    normalize_quotes(item, self._quote_style, target_lang=self._target_lang)
                    if isinstance(item, str)
                    else item
                    for item in meta[key]
                ]
        for entry in (
            meta.get("toc_entries", []) if isinstance(meta.get("toc_entries"), list) else []
        ):
            if isinstance(entry, dict) and isinstance(entry.get("title_translated"), str):
                entry["title_translated"] = normalize_quotes(
                    entry["title_translated"], self._quote_style, target_lang=self._target_lang
                )

    def load_chapter(self, ci: int) -> Chapter:
        """Return the prepared read-only chapter, resolving it on first request.

        The view keeps one resolved chapter per requested index and hands the same
        instance to every reader, so exports stop paying repeated full-chapter deep
        copies. Stores return independent chapter objects per call, so storage stays
        untouched; callers that need to mutate a chapter copy it first with
        ``model_copy(deep=True)``.
        """
        cached = self._chapters.get(ci)
        if cached is None:
            cached = self._store.load_chapter(ci)
            self._resolve_protected_objects(cached)
            self._normalize_chapter_quotes(cached)
            self._chapters[ci] = cached
        return cached

    def iter_chapters_once(self, indices: Iterable[int]) -> Iterator[Chapter]:
        """Yield chapters for one render pass without retaining a second book.

        Snapshot stores copy each chapter per call, so those yields stay independent
        across passes. Live storage and previously prepared chapters share the view
        read-only copy instead of copying again per reader.
        """
        for ci in indices:
            if not isinstance(self._store, ExportSnapshotStore) or ci in self._chapters:
                # Live storage and any previously prepared view keep their cached-read semantics.
                yield self.load_chapter(ci)
                continue
            # The snapshot already returns a deep copy, including nested annotation metadata.
            chapter = self._store.load_chapter(ci)
            if any(segment.target is not None for segment in chapter.segments):
                self._resolve_protected_objects(chapter)
                self._normalize_chapter_quotes(chapter)
            yield chapter

    def _normalize_chapter_quotes(self, chapter: Chapter) -> None:
        """Apply export-only quote normalization to target text without mutating source text."""
        if self._quote_style == "preserve":
            return
        segments = chapter.segments
        position = 0
        while position < len(segments):
            end = position + 1
            while end < len(segments) and segments[end].cont:
                end += 1
            group = segments[position:end]
            position = end
            translated = [seg for seg in group if seg.target is not None]
            if not translated:
                continue
            old_joined = "".join(seg.target or "" for seg in translated)
            new_joined = normalize_quotes(
                old_joined, self._quote_style, target_lang=self._target_lang
            )
            if new_joined == old_joined:
                continue
            # Quote normalization is 1-to-1 (same code-point length), so normalize across
            # the joined paragraph to preserve quote direction across cont=True boundaries.
            if len(new_joined) == len(old_joined):
                offset = 0
                for seg in translated:
                    length = len(seg.target or "")
                    seg.target = new_joined[offset : offset + length]
                    offset += length
            else:
                for seg in translated:
                    seg.target = normalize_quotes(
                        seg.target or "", self._quote_style, target_lang=self._target_lang
                    )
                new_joined = "".join(seg.target or "" for seg in translated)
            old_digest = hashlib.sha256(old_joined.encode("utf-8")).hexdigest()
            new_digest = hashlib.sha256(new_joined.encode("utf-8")).hexdigest()
            for key in ("annotations", "epub_annotations", "pdf_annotations", "docx_styles"):
                block = group[0].meta.get(key)
                if isinstance(block, dict) and block.get("target_digest") == old_digest:
                    block["target_digest"] = new_digest

    def _resolve_protected_objects(self, chapter: Chapter) -> None:
        """Restore object placements without changing saved translations."""
        segments = chapter.text_segments
        position = 0
        while position < len(segments):
            end = position + 1
            while end < len(segments) and segments[end].cont:
                end += 1
            group = segments[position:end]
            position = end
            metadata = annotation_metadata(group[0])
            if not metadata or any(segment.target is None for segment in group):
                continue
            source = "".join(segment.source for segment in group)
            targets = tuple(segment.target or "" for segment in group)
            target = "".join(targets)
            if target == source or not target.strip():
                continue
            anchors = object_anchors(metadata.get("items", []))
            if not anchors:
                continue
            resolved = resolve_objects(targets, anchors, source)
            metadata["placements"] = export_object_placements(metadata, resolved)
            metadata["target_digest"] = hashlib.sha256(
                "".join(resolved.targets).encode("utf-8")
            ).hexdigest()
            metadata["objects_resolved_for_export"] = True
            for segment, text in zip(group, resolved.targets):
                segment.target = text

    def prepare(self) -> None:
        """Validate every transformation before opening or replacing an output file."""
        for row in self.load_manifest()["chapters"]:
            self.load_chapter(row["index"])

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)


AssembleStore: TypeAlias = Storage | ExportViewStore | ExportSnapshotStore
