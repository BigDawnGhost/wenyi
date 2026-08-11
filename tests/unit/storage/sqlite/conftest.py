"""Shared deterministic fixtures for SQLite workflow delivery tests."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from trans_novel.domain.workflow import StageStatus, WorkflowStatus
from trans_novel.storage.sqlite_workflows import SQLiteWorkflowRepository
from trans_novel.storage.sqlite_workflows import repository as repository_module
from trans_novel.workflow import StatePatch, new_workflow_state

SOURCE_HASH = "a" * 64
PROFILE_HASH = "b" * 64


@dataclass
class ManualClock:
    """Expose a mutable millisecond clock so lease boundaries are exact."""

    now_ms: int = 1_000

    def __call__(self) -> int:
        """Return the timestamp currently selected by the test."""
        return self.now_ms


@dataclass
class WorkflowHarness:
    """Bundle one initialized repository with helpers for legal event patches."""

    repository: SQLiteWorkflowRepository
    workflow_id: str
    clock: ManualClock

    def build_event_patch(
        self,
        *,
        operation_id: str,
        event_ids: tuple[str, ...],
    ) -> StatePatch:
        """Build a valid first-stage patch carrying the requested outbox events."""
        state = self.repository.get(self.workflow_id)
        return StatePatch(
            operation_id=operation_id,
            expected_revision=state["revision"],
            updates={
                "status": WorkflowStatus.RUNNING.value,
                "preparation": {
                    "status": StageStatus.RUNNING.value,
                    "normalized_source": None,
                },
            },
            events=tuple(
                {
                    "event_id": event_id,
                    "event_type": "test.delivery-requested",
                    "payload": {"event_id": event_id, "nested": {"unicode": "可恢复"}},
                }
                for event_id in event_ids
            ),
        )


@pytest.fixture
def workflow_harness(tmp_path, monkeypatch) -> WorkflowHarness:
    """Create one pristine workflow repository driven by a deterministic clock."""
    clock = ManualClock()
    monkeypatch.setattr(repository_module, "_now_ms", clock)

    repository = SQLiteWorkflowRepository(tmp_path / "workflow.sqlite3")
    state = new_workflow_state(
        source_artifact={
            "uri": "artifact://source/book.epub",
            "sha256": SOURCE_HASH,
            "media_type": "application/epub+zip",
            "size_bytes": 123,
        },
        source_format="epub",
        source_lang="ja",
        target_lang="zh",
        semantic_profile_hash=PROFILE_HASH,
    )
    repository.create(state)
    return WorkflowHarness(
        repository=repository,
        workflow_id=state["workflow_id"],
        clock=clock,
    )
