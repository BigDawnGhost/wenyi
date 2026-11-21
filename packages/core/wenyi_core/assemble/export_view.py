"""Resolve protected-object markers on disposable export copies."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator
from typing import Any, TypeAlias

from ..alignment.objects import resolve_objects
from ..i18n.policy.models import PolicyContext, PolicyPlan
from ..i18n.policy.resolver import resolve_policy
from ..markup.contexts import annotation_metadata
from ..markup.objects import export_object_placements, object_anchors
from ..models.document import Chapter
from ..storage.protocol import Storage
from ..storage.run_store import ExportSnapshotStore, RunStore
from .writer_common import _manifest_target_lang


class ExportViewStore(RunStore):
    """Overlay read-only export transformations on RunStore and delegate other capabilities."""

    def __init__(
        self,
        store: Storage | ExportSnapshotStore,
        *,
        plan: PolicyPlan | None = None,
    ) -> None:
        super().__init__(store.run_dir, create=False)
        self._store = store
        self._chapters: dict[int, Chapter] = {}
        manifest = store.load_manifest()
        self.language_policy = plan or resolve_policy(
            PolicyContext(
                manifest.get("source_lang", "auto"),
                _manifest_target_lang(manifest),
                phase="export",
                format="epub",
                source_identity=manifest.get("source_sha256", ""),
            )
        )

    def load_manifest(self) -> dict:
        return self._store.load_manifest()

    def load_chapter(self, ci: int) -> Chapter:
        if ci in self._chapters:
            return self._chapters[ci].model_copy(deep=True)
        chapter = self._store.load_chapter(ci).model_copy(deep=True)
        self._resolve_protected_objects(chapter)
        self._chapters[ci] = chapter.model_copy(deep=True)
        return chapter

    def iter_chapters_once(self, indices: Iterable[int]) -> Iterator[Chapter]:
        """Yield independent render copies without retaining a second book in the view."""
        for ci in indices:
            if not isinstance(self._store, ExportSnapshotStore) or ci in self._chapters:
                # Live storage and any previously prepared view keep their cached-read semantics.
                yield self.load_chapter(ci)
                continue
            # The snapshot already returns a deep copy, including nested annotation metadata.
            chapter = self._store.load_chapter(ci)
            if any(segment.target is not None for segment in chapter.segments):
                self._resolve_protected_objects(chapter)
            yield chapter

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
