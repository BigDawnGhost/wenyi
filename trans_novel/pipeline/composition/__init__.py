"""Pipeline composition capability public API."""

from __future__ import annotations

from trans_novel.pipeline.composition.agents import AgentBundle
from trans_novel.pipeline.composition.context import RunContext
from trans_novel.pipeline.composition.nodes import build_node_factory
from trans_novel.pipeline.composition.note_compatibility import (
    ensure_epub_note_compatibility,
)
from trans_novel.pipeline.composition.note_service import (
    ensure_service_epub_note_compatibility,
)

__all__ = [
    "AgentBundle",
    "RunContext",
    "build_node_factory",
    "ensure_epub_note_compatibility",
    "ensure_service_epub_note_compatibility",
]
