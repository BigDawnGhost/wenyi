"""Disposable chapter view used only for export.
Deterministic postprocessing changes only in-memory copies returned by load_chapter and
never writes back to RunStore.
"""

from __future__ import annotations

import hashlib
from difflib import SequenceMatcher
from typing import Any

from ..ingest.models import Chapter
from ..pipeline.runstore import RunStore
from ..postprocess.punct import normalize_zh_segments
from .writer_common import _manifest_target_lang


def _target_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _boundary_map(before: str, after: str) -> list[int]:
    """Map original character boundaries to transformed boundaries for annotation and style
    offsets.
    """
    mapping = [0] * (len(before) + 1)
    matcher = SequenceMatcher(a=before, b=after, autojunk=False)
    for operation, before_start, before_end, after_start, after_end in matcher.get_opcodes():
        before_length = before_end - before_start
        after_length = after_end - after_start
        if operation == "equal":
            for offset in range(before_length + 1):
                mapping[before_start + offset] = after_start + offset
        elif operation == "insert":
            mapping[before_start] = after_end
        else:
            for offset in range(before_length + 1):
                mapped_offset = (offset * after_length + before_length // 2) // before_length
                mapping[before_start + offset] = after_start + mapped_offset
    return mapping


def _remap_metadata_offsets(metadata: object, before: str, after: str) -> None:
    """Remap export-copy offsets only when placements match the formal translation."""
    if not isinstance(metadata, dict) or metadata.get("target_digest") != _target_digest(before):
        return
    mapping = _boundary_map(before, after)
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

    def __init__(self, store: RunStore, *, punctuation_normalize: bool) -> None:
        super().__init__(store.run_dir, create=False)
        self._store = store
        self._punctuation_normalize = (
            punctuation_normalize and _manifest_target_lang(store.load_manifest()) == "zh"
        )

    def load_manifest(self) -> dict:
        return self._store.load_manifest()

    def load_chapter(self, ci: int) -> Chapter:
        chapter = self._store.load_chapter(ci)
        if not self._punctuation_normalize:
            return chapter

        segments = chapter.text_segments
        normalized = normalize_zh_segments(
            [segment.target or "" for segment in segments],
            [segment.cont for segment in segments],
        )
        position = 0
        while position < len(segments):
            end = position + 1
            while end < len(segments) and segments[end].cont:
                end += 1
            before = "".join(segment.target or "" for segment in segments[position:end])
            after = "".join(normalized[position:end])
            if before != after:
                metadata = segments[position].meta
                _remap_metadata_offsets(metadata.get("epub_annotations"), before, after)
                _remap_metadata_offsets(metadata.get("docx_styles"), before, after)
            position = end

        for segment, target in zip(segments, normalized):
            if segment.target is not None:
                segment.target = target
        return chapter

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)
