"""Adapt Review persistence to the narrow ports consumed by agents."""

from __future__ import annotations

import re
from typing import Any

from ..review.run_store import ReviewRunStore


class ReviewTraceStore:
    """Expose agent traces within one Review and its caller-owned active round scope."""

    def __init__(self, store: ReviewRunStore) -> None:
        self._store = store

    @staticmethod
    def _relative(agent_id: str) -> str:
        safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", agent_id).strip("-") or "agent"
        return f"agents/{safe_id}.json"

    def load(self, agent_id: str) -> dict[str, Any] | None:
        """Read the existing trace using the unchanged filename and round rules."""
        return self._store.load_json(self._relative(agent_id))

    def save(self, agent_id: str, snapshot: dict[str, Any]) -> None:
        """Commit immediately through the existing atomic JSON writer."""
        self._store.write_json(self._relative(agent_id), snapshot)

    def log_event(self, event: str, **data: Any) -> None:
        """Preserve event ordering, locking and active-round annotations."""
        self._store.log_event(event, **data)
