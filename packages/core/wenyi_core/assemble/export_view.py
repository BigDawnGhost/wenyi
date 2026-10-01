"""Disposable chapter view used only for export.
Deterministic postprocessing changes only in-memory copies returned by load_chapter and
never writes back to RunStore.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from types import MappingProxyType
from typing import Any, TypeAlias

from ..i18n.policy.models import ExportTextInput, ExportTextResult, PolicyContext, PolicyPlan
from ..i18n.policy.resolver import resolve_policy
from ..ingest.models import Chapter
from ..pipeline.runstore import ExportSnapshotStore, RunStore
from ..postprocess.export_text import boundary_map, normalize_chinese, validate_result
from ..storage.protocol import Storage
from .writer_common import _manifest_target_lang

TEXT_HANDLERS: MappingProxyType[str, Callable[[ExportTextInput], ExportTextResult]] = (
    MappingProxyType(
        {
            "punctuation.zh_cn": normalize_chinese,
        }
    )
)


def _target_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _boundary_map(before: str, after: str) -> list[int]:
    """Map original character boundaries to transformed boundaries for annotation and style
    offsets.
    """
    return list(boundary_map(before, after))


def _remap_metadata_offsets(
    metadata: object, before: str, after: str, mapping: tuple[int, ...] | None = None
) -> None:
    """Remap export-copy offsets only when placements match the formal translation."""
    if not isinstance(metadata, dict) or metadata.get("target_digest") != _target_digest(before):
        return
    mapping = mapping if mapping is not None else boundary_map(before, after)
    raw_placements = metadata.get("placements")
    if isinstance(raw_placements, list):
        for placement in raw_placements:
            if not isinstance(placement, dict):
                continue
            start = placement.get("target_start")
            end = placement.get("target_end")
            if (
                not isinstance(start, int)
                or isinstance(start, bool)
                or not isinstance(end, int)
                or isinstance(end, bool)
            ):
                continue
            start = min(max(start, 0), len(before))
            end = min(max(end, start), len(before))
            placement["target_start"] = mapping[start]
            placement["target_end"] = mapping[end]
    metadata["target_digest"] = _target_digest(after)


class ExportViewStore(RunStore):
    """Overlay read-only export transformations on RunStore and delegate other capabilities."""

    def __init__(
        self,
        store: Storage | ExportSnapshotStore,
        *,
        punctuation_normalize: bool,
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
                punctuation_normalize=punctuation_normalize,
                source_identity=manifest.get("source_sha256", ""),
            )
        )

    def load_manifest(self) -> dict:
        return self._store.load_manifest()

    def load_chapter(self, ci: int) -> Chapter:
        if ci in self._chapters:
            return self._chapters[ci].model_copy(deep=True)
        chapter = self._store.load_chapter(ci).model_copy(deep=True)
        operations = [op for op in self.language_policy.operations if op.point == "export.text"]
        if not operations:
            self._chapters[ci] = chapter.model_copy(deep=True)
            return chapter

        segments = chapter.text_segments
        ranges = []
        position = 0
        while position < len(segments):
            end = position + 1
            while end < len(segments) and segments[end].cont:
                end += 1
            ranges.append((position, end))
            position = end
        request = ExportTextInput(
            tuple(segment.index for segment in segments),
            tuple(
                segment.target if segment.target != segment.source else None for segment in segments
            ),
            tuple(segment.cont for segment in segments),
            tuple(ranges),
        )
        current = request
        maps = tuple(
            tuple(range(len("".join(text or "" for text in request.targets[start:end])) + 1))
            for start, end in ranges
        )
        for operation in operations:
            result = TEXT_HANDLERS[operation.id](current)
            validate_result(current, result)
            maps = tuple(
                tuple(after[index] for index in before)
                for before, after in zip(maps, result.logical_boundary_maps)
            )
            current = ExportTextInput(
                result.segment_ids, result.targets, result.continuations, result.logical_ranges
            )
        for (position, end), mapping in zip(ranges, maps):
            before = "".join(segment.target or "" for segment in segments[position:end])
            after = "".join(target or "" for target in current.targets[position:end])
            if any(segment.target == segment.source for segment in segments[position:end]):
                continue
            if before != after:
                metadata = segments[position].meta
                _remap_metadata_offsets(metadata.get("epub_annotations"), before, after, mapping)
                _remap_metadata_offsets(metadata.get("docx_styles"), before, after, mapping)

        for segment, target in zip(segments, current.targets):
            if segment.target is not None and segment.target != segment.source:
                segment.target = target
        self._chapters[ci] = chapter.model_copy(deep=True)
        return chapter

    def prepare(self) -> None:
        """Validate every transformation before opening or replacing an output file."""
        for row in self.load_manifest()["chapters"]:
            self.load_chapter(row["index"])

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)


AssembleStore: TypeAlias = Storage | ExportViewStore | ExportSnapshotStore
