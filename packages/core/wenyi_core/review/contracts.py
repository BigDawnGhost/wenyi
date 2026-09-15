"""Narrow trace and evidence ports used by the Review conversation protocol."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from .models import SegmentRef

if TYPE_CHECKING:
    from ..glossary.store import GlossaryTerm


class ReviewTrace(Protocol):
    """Persist agent snapshots and diagnostics without exposing paths or book state."""

    def load(self, agent_id: str) -> dict[str, Any] | None: ...

    def save(self, agent_id: str, snapshot: dict[str, Any]) -> None: ...

    def log_event(self, event: str, **data: Any) -> None: ...


class EvidenceTools(Protocol):
    """Execute bounded evidence requests and identify the references they supplied."""

    def execute(self, request: dict[str, Any]) -> dict[str, Any]: ...

    def evidence_refs(self, value: Any) -> set[str]: ...


class EvidenceQueries(EvidenceTools, Protocol):
    """Read stable locations and canonical terms for prompt preparation and conflicts."""

    def segment_ref(self, chapter: int, text_index: int) -> SegmentRef | None: ...

    def canonical_term(self, query: str) -> tuple[GlossaryTerm | None, list[str]]: ...
