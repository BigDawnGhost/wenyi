"""Tests for the framework-neutral, repository-authoritative phase boundary."""

from __future__ import annotations

import subprocess
import sys
from copy import deepcopy
from dataclasses import FrozenInstanceError
from typing import cast

import pytest

from trans_novel.application.runtime import ExecutionContext
from trans_novel.application.workflow_execution import (
    GraphObservation,
    UnsupportedWorkflowPhase,
    WorkflowDidNotProgress,
    WorkflowPhaseRunners,
    execute_current_phase,
    hydrate,
)
from trans_novel.domain.workflow import WorkflowPhase, WorkflowStatus
from trans_novel.workflow.repository import ArtifactStore, WorkflowRepository
from trans_novel.workflow.state import WorkflowState


def _state(
    *,
    revision: int = 4,
    status: str = WorkflowStatus.RUNNING.value,
    phase: str = WorkflowPhase.PREPARE.value,
) -> WorkflowState:
    """Build the minimal projection needed by this boundary-only unit test."""
    return cast(
        WorkflowState,
        {
            "workflow_id": "wf-" + "a" * 64,
            "revision": revision,
            "status": status,
            "cursor": {"phase": phase},
        },
    )


class _RepositoryProbe:
    """Return detached snapshots and expose an explicit simulated commit step."""

    def __init__(self, *snapshots: WorkflowState) -> None:
        if not snapshots:
            raise ValueError("at least one snapshot is required")
        self._snapshots = [deepcopy(snapshot) for snapshot in snapshots]
        self._position = 0
        self.get_calls: list[str] = []

    def get(self, workflow_id: str) -> WorkflowState:
        self.get_calls.append(workflow_id)
        return deepcopy(self._snapshots[self._position])

    def commit_next(self) -> None:
        if self._position + 1 >= len(self._snapshots):
            raise AssertionError("test runner committed beyond configured snapshots")
        self._position += 1


class _RunnerProbe:
    """Record injected ports and optionally advance the fake repository."""

    def __init__(self, *, commit: bool = False) -> None:
        self.commit = commit
        self.calls: list[
            tuple[WorkflowState, WorkflowRepository, ArtifactStore, ExecutionContext]
        ] = []

    def __call__(
        self,
        state: WorkflowState,
        *,
        repository: WorkflowRepository,
        artifacts: ArtifactStore,
        context: ExecutionContext,
    ) -> None:
        self.calls.append((state, repository, artifacts, context))
        if self.commit:
            cast(_RepositoryProbe, repository).commit_next()


def _runners(
    selected_phase: str | None = None,
    *,
    commit: bool = False,
) -> tuple[WorkflowPhaseRunners, dict[str, _RunnerProbe]]:
    """Create a complete registry and return it with its per-phase probes."""
    probes = {
        phase.value: _RunnerProbe(commit=commit and phase.value == selected_phase)
        for phase in WorkflowPhase
        if phase is not WorkflowPhase.COMPLETE
    }
    return (
        WorkflowPhaseRunners(
            prepare=probes[WorkflowPhase.PREPARE.value],
            understand=probes[WorkflowPhase.UNDERSTAND.value],
            translate_chapters=probes[WorkflowPhase.TRANSLATE_CHAPTERS.value],
            translate_titles=probes[WorkflowPhase.TRANSLATE_TITLES.value],
            review=probes[WorkflowPhase.REVIEW.value],
            quality=probes[WorkflowPhase.QUALITY.value],
            export=probes[WorkflowPhase.EXPORT.value],
        ),
        probes,
    )


def test_hydrate_always_projects_a_fresh_repository_snapshot() -> None:
    repository = _RepositoryProbe(
        _state(revision=9, status=WorkflowStatus.PAUSED.value, phase=WorkflowPhase.REVIEW.value)
    )

    observation = hydrate("wf-" + "a" * 64, repository=repository)

    assert observation == GraphObservation(
        workflow_id="wf-" + "a" * 64,
        revision=9,
        status="paused",
        phase="review",
    )
    assert repository.get_calls == ["wf-" + "a" * 64]


@pytest.mark.parametrize(
    "status",
    [
        WorkflowStatus.PAUSED.value,
        WorkflowStatus.FAILED.value,
        WorkflowStatus.COMPLETED.value,
    ],
)
def test_non_executable_statuses_return_without_calling_any_runner(status: str) -> None:
    phase = WorkflowPhase.COMPLETE.value if status == "completed" else WorkflowPhase.REVIEW.value
    repository = _RepositoryProbe(_state(status=status, phase=phase))
    runners, probes = _runners()

    observation = execute_current_phase(
        "wf-" + "a" * 64,
        repository=repository,
        artifacts=cast(ArtifactStore, object()),
        context=ExecutionContext(run_id="terminal"),
        runners=runners,
    )

    assert observation.status == status
    assert repository.get_calls == ["wf-" + "a" * 64]
    assert all(not probe.calls for probe in probes.values())


@pytest.mark.parametrize(
    "phase",
    [phase.value for phase in WorkflowPhase if phase is not WorkflowPhase.COMPLETE],
)
def test_each_active_phase_receives_the_detached_state_and_narrow_ports(phase: str) -> None:
    before = _state(phase=phase)
    after = _state(revision=5, phase=phase)
    repository = _RepositoryProbe(before, after)
    artifacts = cast(ArtifactStore, object())
    context = ExecutionContext(run_id=f"phase:{phase}")
    runners, probes = _runners(phase, commit=True)

    observation = execute_current_phase(
        before["workflow_id"],
        repository=repository,
        artifacts=artifacts,
        context=context,
        runners=runners,
    )

    assert observation == GraphObservation(before["workflow_id"], 5, "running", phase)
    assert repository.get_calls == [before["workflow_id"], before["workflow_id"]]
    selected = probes[phase]
    assert len(selected.calls) == 1
    state_arg, repository_arg, artifacts_arg, context_arg = selected.calls[0]
    assert state_arg == before
    assert repository_arg is repository
    assert artifacts_arg is artifacts
    assert context_arg is context
    assert all(not probe.calls for name, probe in probes.items() if name != phase)


def test_pending_preparation_can_commit_the_initial_running_transition() -> None:
    before = _state(revision=0, status=WorkflowStatus.PENDING.value)
    after = _state(revision=1, status=WorkflowStatus.RUNNING.value)
    repository = _RepositoryProbe(before, after)
    runners, probes = _runners(WorkflowPhase.PREPARE.value, commit=True)

    observation = execute_current_phase(
        before["workflow_id"],
        repository=repository,
        artifacts=cast(ArtifactStore, object()),
        context=ExecutionContext(run_id="start"),
        runners=runners,
    )

    assert observation == GraphObservation(before["workflow_id"], 1, "running", "prepare")
    assert len(probes[WorkflowPhase.PREPARE.value].calls) == 1


def test_runner_result_is_ignored_in_favor_of_the_post_commit_reload() -> None:
    before = _state(phase=WorkflowPhase.UNDERSTAND.value)
    after = _state(
        revision=5,
        status=WorkflowStatus.COMPLETED.value,
        phase=WorkflowPhase.COMPLETE.value,
    )
    repository = _RepositoryProbe(before, after)
    runners, _ = _runners(WorkflowPhase.UNDERSTAND.value, commit=True)

    observation = execute_current_phase(
        before["workflow_id"],
        repository=repository,
        artifacts=cast(ArtifactStore, object()),
        context=ExecutionContext(run_id="complete"),
        runners=runners,
    )

    assert observation == GraphObservation(before["workflow_id"], 5, "completed", "complete")


@pytest.mark.parametrize(
    "status",
    [WorkflowStatus.PENDING.value, WorkflowStatus.RUNNING.value],
)
def test_active_phase_that_does_not_commit_is_rejected(status: str) -> None:
    state = _state(revision=7, status=status, phase=WorkflowPhase.QUALITY.value)
    repository = _RepositoryProbe(state)
    runners, _ = _runners()

    with pytest.raises(WorkflowDidNotProgress, match="quality.*revision 7"):
        execute_current_phase(
            state["workflow_id"],
            repository=repository,
            artifacts=cast(ArtifactStore, object()),
            context=ExecutionContext(run_id="stalled"),
            runners=runners,
        )

    assert repository.get_calls == [state["workflow_id"], state["workflow_id"]]


def test_same_revision_is_rejected_even_if_fake_repository_changes_routing_fields() -> None:
    before = _state(revision=4, phase=WorkflowPhase.QUALITY.value)
    after = _state(revision=4, status=WorkflowStatus.PAUSED.value, phase=WorkflowPhase.EXPORT.value)
    repository = _RepositoryProbe(before, after)
    runners, _ = _runners(WorkflowPhase.QUALITY.value, commit=True)

    # A conforming repository cannot change status or phase without committing
    # a new revision.  Reject the contradictory fake snapshot instead of hiding
    # storage corruption behind a superficially valid routing transition.
    with pytest.raises(WorkflowDidNotProgress, match="quality.*revision 4"):
        execute_current_phase(
            before["workflow_id"],
            repository=repository,
            artifacts=cast(ArtifactStore, object()),
            context=ExecutionContext(run_id="boundary"),
            runners=runners,
        )


def test_active_complete_or_unknown_phase_fails_before_a_runner_call() -> None:
    runners, probes = _runners()
    for phase in (WorkflowPhase.COMPLETE.value, "future-phase"):
        repository = _RepositoryProbe(_state(phase=phase))
        with pytest.raises(UnsupportedWorkflowPhase, match=repr(phase)):
            execute_current_phase(
                "wf-" + "a" * 64,
                repository=repository,
                artifacts=cast(ArtifactStore, object()),
                context=ExecutionContext(run_id="bad-phase"),
                runners=runners,
            )

    assert all(not probe.calls for probe in probes.values())


def test_runner_bundle_is_frozen() -> None:
    runners, probes = _runners()

    with pytest.raises(FrozenInstanceError):
        runners.prepare = probes[WorkflowPhase.REVIEW.value]  # type: ignore[misc]


def test_clean_import_has_no_graph_legacy_or_concrete_storage_dependencies() -> None:
    script = """
import sys
import trans_novel.application.workflow_execution

forbidden = (
    "langgraph",
    "trans_novel.cli",
    "trans_novel.config",
    "trans_novel.llm",
    "trans_novel.pipeline",
    "trans_novel.pipeline.orchestrator",
    "trans_novel.pipeline.runstore",
    "trans_novel.storage",
    "trans_novel.storage.sqlite_workflows",
    "pydantic",
)
loaded = [name for name in forbidden if name in sys.modules]
if loaded:
    raise SystemExit(f"unexpected workflow execution dependencies: {loaded}")
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
