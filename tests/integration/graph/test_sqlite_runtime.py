"""Integration tests for SQLite checkpoint ownership and process-style reopen."""

from __future__ import annotations

import os
import sqlite3
from copy import deepcopy
from pathlib import Path
from typing import cast

import pytest

from trans_novel.application.runtime import ExecutionContext
from trans_novel.application.workflow_execution import WorkflowPhaseRunners
from trans_novel.domain.workflow import WorkflowPhase, WorkflowStatus
from trans_novel.graph.runtime import (
    CHECKPOINT_DATABASE_NAME,
    DEFAULT_RECURSION_LIMIT,
    open_workflow_graph_runtime,
)
from trans_novel.workflow.repository import ArtifactStore, WorkflowRepository
from trans_novel.workflow.state import WorkflowState

WORKFLOW_ID = "wf-" + "c" * 64


class _Repository:
    """Small repository probe whose state survives runtime reopen in one test."""

    def __init__(self, state: WorkflowState) -> None:
        self.state = deepcopy(state)

    def get(self, workflow_id: str) -> WorkflowState:
        assert workflow_id == WORKFLOW_ID
        return deepcopy(self.state)


class _CompleteRunner:
    """Advance the one active phase to a durable completed observation."""

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def __call__(
        self,
        state: WorkflowState,
        *,
        repository: WorkflowRepository,
        artifacts: ArtifactStore,
        context: ExecutionContext,
    ) -> None:
        del state, artifacts
        fake = cast(_Repository, repository)
        self.calls.append(context.run_id)
        fake.state["revision"] += 1
        fake.state["status"] = WorkflowStatus.COMPLETED.value
        fake.state["cursor"]["phase"] = WorkflowPhase.COMPLETE.value


class _ResumeAfterCrashRunner:
    """Persist one batch, crash, then resume from that revision after reopen."""

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def __call__(
        self,
        state: WorkflowState,
        *,
        repository: WorkflowRepository,
        artifacts: ArtifactStore,
        context: ExecutionContext,
    ) -> None:
        del artifacts
        fake = cast(_Repository, repository)
        self.calls.append(f"{state['cursor']['phase']}:{state['revision']}:{context.run_id}")
        fake.state["revision"] += 1
        if state["revision"] == 0:
            raise RuntimeError("simulated process crash after business batch commit")
        fake.state["cursor"]["phase"] = WorkflowPhase.UNDERSTAND.value


def test_sqlite_runtime_reopens_same_thread_and_completed_reinvoke_is_inert(
    tmp_path: Path,
) -> None:
    repository = _Repository(
        cast(
            WorkflowState,
            {
                "workflow_id": WORKFLOW_ID,
                "revision": 0,
                "status": WorkflowStatus.RUNNING.value,
                "cursor": {"phase": WorkflowPhase.EXPORT.value},
            },
        )
    )
    calls: list[str] = []
    runner = _CompleteRunner(calls)
    runners = WorkflowPhaseRunners(
        prepare=runner,
        understand=runner,
        translate_chapters=runner,
        translate_titles=runner,
        review=runner,
        quality=runner,
        export=runner,
    )
    artifacts = cast(ArtifactStore, object())
    business_marker = tmp_path / "workflow.sqlite3"
    business_marker.write_bytes(b"authoritative-owner-v1")

    with open_workflow_graph_runtime(
        tmp_path,
        repository=cast(WorkflowRepository, repository),
        artifacts=artifacts,
        runners=runners,
    ) as runtime:
        first = runtime.invoke(WORKFLOW_ID, execution=ExecutionContext(run_id="first"))

    assert first["status"] == "completed"
    assert calls == ["first"]
    assert (tmp_path / CHECKPOINT_DATABASE_NAME).is_file()
    assert business_marker.read_bytes() == b"authoritative-owner-v1"

    # Reopening the saver and invoking the same thread with plain input starts at
    # hydrate; authoritative completion prevents the old execute node from running.
    with open_workflow_graph_runtime(
        tmp_path,
        repository=cast(WorkflowRepository, repository),
        artifacts=artifacts,
        runners=runners,
    ) as runtime:
        second = runtime.invoke(WORKFLOW_ID, execution=ExecutionContext(run_id="second"))

    assert second == first
    assert calls == ["first"]
    assert business_marker.read_bytes() == b"authoritative-owner-v1"


def test_runtime_requires_existing_business_owner_before_creating_checkpoint(
    tmp_path: Path,
) -> None:
    repository = _Repository(
        cast(
            WorkflowState,
            {
                "workflow_id": WORKFLOW_ID,
                "revision": 2,
                "status": "completed",
                "cursor": {"phase": "complete"},
            },
        )
    )
    runner = _CompleteRunner([])
    runners = WorkflowPhaseRunners(
        prepare=runner,
        understand=runner,
        translate_chapters=runner,
        translate_titles=runner,
        review=runner,
        quality=runner,
        export=runner,
    )

    with pytest.raises(ValueError, match="workflow repository marker does not exist"):
        with open_workflow_graph_runtime(
            tmp_path,
            repository=cast(WorkflowRepository, repository),
            artifacts=cast(ArtifactStore, object()),
            runners=runners,
        ):
            raise AssertionError("runtime must not open without its business owner")

    assert not (tmp_path / CHECKPOINT_DATABASE_NAME).exists()


def test_runtime_rejects_checkpoint_hardlinked_to_business_database(tmp_path: Path) -> None:
    business_marker = tmp_path / "workflow.sqlite3"
    business_marker.write_bytes(b"business-owner")
    try:
        os.link(business_marker, tmp_path / CHECKPOINT_DATABASE_NAME)
    except OSError as error:  # pragma: no cover - platform policy, not product behavior
        pytest.skip(f"hard links are unavailable: {error}")

    runner = _CompleteRunner([])
    runners = WorkflowPhaseRunners(*(runner for _ in range(7)))
    with pytest.raises(ValueError, match="cannot alias"):
        with open_workflow_graph_runtime(
            tmp_path,
            repository=cast(WorkflowRepository, object()),
            artifacts=cast(ArtifactStore, object()),
            runners=runners,
        ):
            raise AssertionError("aliased databases must never open")


def test_runtime_rejects_checkpoint_symlink(tmp_path: Path) -> None:
    business_marker = tmp_path / "workflow.sqlite3"
    business_marker.write_bytes(b"business-owner")
    checkpoint_path = tmp_path / CHECKPOINT_DATABASE_NAME
    try:
        checkpoint_path.symlink_to(business_marker)
    except OSError as error:  # pragma: no cover - Windows privilege policy
        pytest.skip(f"symbolic links are unavailable: {error}")

    runner = _CompleteRunner([])
    runners = WorkflowPhaseRunners(*(runner for _ in range(7)))
    with pytest.raises(ValueError, match="physical regular file"):
        with open_workflow_graph_runtime(
            tmp_path,
            repository=cast(WorkflowRepository, object()),
            artifacts=cast(ArtifactStore, object()),
            runners=runners,
        ):
            raise AssertionError("linked checkpoint databases must never open")


def test_runtime_rejects_business_schema_inside_checkpoint_database(tmp_path: Path) -> None:
    (tmp_path / "workflow.sqlite3").touch()
    checkpoint_path = tmp_path / CHECKPOINT_DATABASE_NAME
    with sqlite3.connect(checkpoint_path) as connection:
        connection.execute("CREATE TABLE workflow_snapshots (workflow_id TEXT)")

    runner = _CompleteRunner([])
    runners = WorkflowPhaseRunners(*(runner for _ in range(7)))
    with pytest.raises(ValueError, match="workflow repository tables"):
        with open_workflow_graph_runtime(
            tmp_path,
            repository=cast(WorkflowRepository, object()),
            artifacts=cast(ArtifactStore, object()),
            runners=runners,
        ):
            raise AssertionError("mixed business/checkpoint schema must never open")


def test_sqlite_reopen_public_invoke_resumes_business_progress_after_crash(
    tmp_path: Path,
) -> None:
    (tmp_path / "workflow.sqlite3").write_bytes(b"authoritative-owner-v1")
    repository = _Repository(
        cast(
            WorkflowState,
            {
                "workflow_id": WORKFLOW_ID,
                "revision": 0,
                "status": WorkflowStatus.RUNNING.value,
                "cursor": {"phase": WorkflowPhase.PREPARE.value},
            },
        )
    )
    calls: list[str] = []
    prepare = _ResumeAfterCrashRunner(calls)
    complete = _CompleteRunner(calls)
    runners = WorkflowPhaseRunners(
        prepare=prepare,
        understand=complete,
        translate_chapters=complete,
        translate_titles=complete,
        review=complete,
        quality=complete,
        export=complete,
    )
    ports = {
        "repository": cast(WorkflowRepository, repository),
        "artifacts": cast(ArtifactStore, object()),
        "runners": runners,
    }

    with open_workflow_graph_runtime(tmp_path, **ports) as runtime:
        with pytest.raises(RuntimeError, match="after business batch commit"):
            runtime.invoke(WORKFLOW_ID, execution=ExecutionContext(run_id="before-crash"))

    with open_workflow_graph_runtime(tmp_path, **ports) as runtime:
        result = runtime.invoke(WORKFLOW_ID, execution=ExecutionContext(run_id="after-reopen"))
        history = runtime.graph.get_state_history({"configurable": {"thread_id": WORKFLOW_ID}})
        business_values = [snapshot.values for snapshot in history if snapshot.values]

    assert result == {
        "workflow_id": WORKFLOW_ID,
        "revision": 3,
        "status": "completed",
        "phase": "complete",
    }
    assert calls == [
        "prepare:0:before-crash",
        "prepare:1:after-reopen",
        "after-reopen",
    ]
    assert all(
        set(values) == {"workflow_id", "revision", "status", "phase"} for values in business_values
    )


def test_project_recursion_safety_limit_has_headroom_for_phase_routing() -> None:
    """Keep a project guard far above the roughly seven phase transitions."""
    assert DEFAULT_RECURSION_LIMIT == 1000
