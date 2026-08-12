"""Unit tests for the real StateGraph adapter and its replay boundaries."""

from __future__ import annotations

import subprocess
import sys
from copy import deepcopy
from typing import Any, cast

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.runtime import Runtime

from trans_novel.application.runtime import ExecutionContext
from trans_novel.application.workflow_execution import GraphObservation, WorkflowPhaseRunners
from trans_novel.domain.workflow import WorkflowPhase, WorkflowStatus
from trans_novel.graph import adapter as adapter_module
from trans_novel.graph.adapter import (
    WorkflowGraphContext,
    build_workflow_graph,
    execute_phase_node,
)
from trans_novel.graph.state import WorkflowGraphState
from trans_novel.workflow.repository import ArtifactStore, WorkflowRepository
from trans_novel.workflow.state import WorkflowState

WORKFLOW_ID = "wf-" + "a" * 64


def _state(
    revision: int,
    status: str,
    phase: str,
) -> WorkflowState:
    """Build the routing subset consumed through the repository port."""
    return cast(
        WorkflowState,
        {
            "workflow_id": WORKFLOW_ID,
            "revision": revision,
            "status": status,
            "cursor": {"phase": phase},
        },
    )


class _Repository:
    """Mutable authoritative snapshot controlled by phase runners in tests."""

    def __init__(self, state: WorkflowState) -> None:
        self.state = deepcopy(state)
        self.get_count = 0

    def get(self, workflow_id: str) -> WorkflowState:
        assert workflow_id == WORKFLOW_ID
        self.get_count += 1
        return deepcopy(self.state)


class _AdvancingRunner:
    """Commit one deterministic routing transition into the fake repository."""

    def __init__(
        self,
        next_status: str,
        next_phase: str,
        calls: list[str],
    ) -> None:
        self.next_status = next_status
        self.next_phase = next_phase
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
        self.calls.append(f"{state['cursor']['phase']}:{context.run_id}")
        fake.state["revision"] += 1
        fake.state["status"] = self.next_status
        fake.state["cursor"]["phase"] = self.next_phase


class _CommitThenCrashRunner:
    """Crash after one batch, then resume that phase from repository progress."""

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
        del artifacts, context
        fake = cast(_Repository, repository)
        self.calls.append(f"{state['cursor']['phase']}:{state['revision']}")
        fake.state["revision"] += 1
        if state["revision"] == 0:
            # The first internal batch is durable but the phase is unfinished.
            raise RuntimeError("simulated crash after durable batch commit")
        fake.state["cursor"]["phase"] = WorkflowPhase.UNDERSTAND.value


def _context(
    repository: _Repository,
    transitions: dict[str, tuple[str, str]],
    calls: list[str],
    *,
    run_id: str = "graph-run",
) -> WorkflowGraphContext:
    """Build all seven runners while selecting transitions by current phase."""

    def runner(phase: str) -> _AdvancingRunner:
        next_status, next_phase = transitions.get(
            phase,
            (WorkflowStatus.COMPLETED.value, WorkflowPhase.COMPLETE.value),
        )
        return _AdvancingRunner(next_status, next_phase, calls)

    return WorkflowGraphContext(
        repository=cast(WorkflowRepository, repository),
        artifacts=cast(ArtifactStore, object()),
        execution=ExecutionContext(run_id=run_id),
        runners=WorkflowPhaseRunners(
            prepare=runner(WorkflowPhase.PREPARE.value),
            understand=runner(WorkflowPhase.UNDERSTAND.value),
            translate_chapters=runner(WorkflowPhase.TRANSLATE_CHAPTERS.value),
            translate_titles=runner(WorkflowPhase.TRANSLATE_TITLES.value),
            review=runner(WorkflowPhase.REVIEW.value),
            quality=runner(WorkflowPhase.QUALITY.value),
            export=runner(WorkflowPhase.EXPORT.value),
        ),
    )


def _invoke(graph, input_state: WorkflowGraphState, context: WorkflowGraphContext):
    """Invoke with the same durable settings used by the production wrapper."""
    return graph.invoke(
        input_state,
        config={"configurable": {"thread_id": WORKFLOW_ID}, "recursion_limit": 32},
        context=context,
        durability="sync",
    )


def test_graph_runs_phases_sequentially_and_checkpoints_only_four_fields() -> None:
    repository = _Repository(_state(0, "pending", "prepare"))
    calls: list[str] = []
    context = _context(
        repository,
        {
            "prepare": ("running", "understand"),
            "understand": ("completed", "complete"),
        },
        calls,
    )
    graph = build_workflow_graph(checkpointer=InMemorySaver())

    result = _invoke(
        graph,
        cast(
            WorkflowGraphState,
            {"workflow_id": WORKFLOW_ID, "revision": 99, "status": "failed", "phase": "review"},
        ),
        context,
    )

    assert result == {
        "workflow_id": WORKFLOW_ID,
        "revision": 2,
        "status": "completed",
        "phase": "complete",
    }
    assert calls == ["prepare:graph-run", "understand:graph-run"]
    config = {"configurable": {"thread_id": WORKFLOW_ID}}
    # LangGraph may retain a framework START cursor with no channel values.  All
    # snapshots containing business routing data must keep the exact projection.
    business_snapshots = [
        snapshot.values for snapshot in graph.get_state_history(config) if snapshot.values
    ]
    assert business_snapshots
    assert all(
        set(values) == {"workflow_id", "revision", "status", "phase"}
        for values in business_snapshots
    )


@pytest.mark.parametrize("status", ["paused", "failed", "completed"])
def test_terminal_or_inert_repository_state_ends_without_runner(status: str) -> None:
    phase = "complete" if status == "completed" else "review"
    repository = _Repository(_state(8, status, phase))
    calls: list[str] = []
    context = _context(repository, {}, calls)
    graph = build_workflow_graph(checkpointer=InMemorySaver())

    result = _invoke(
        graph,
        cast(
            WorkflowGraphState,
            {"workflow_id": WORKFLOW_ID, "revision": 0, "status": "running", "phase": "prepare"},
        ),
        context,
    )

    assert result["status"] == status
    assert calls == []


def test_execute_node_passes_checkpoint_observation_as_replay_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _Repository(_state(3, "running", "review"))
    context = _context(repository, {}, [])
    checkpoint = cast(
        WorkflowGraphState,
        {
            "workflow_id": WORKFLOW_ID,
            "revision": 3,
            "status": "running",
            "phase": "review",
        },
    )
    captured: dict[str, Any] = {}

    def fake_execute_current_phase(workflow_id: str, **kwargs: Any) -> GraphObservation:
        captured["workflow_id"] = workflow_id
        captured.update(kwargs)
        return GraphObservation(WORKFLOW_ID, 3, "running", "review")

    monkeypatch.setattr(
        adapter_module,
        "execute_current_phase",
        fake_execute_current_phase,
    )

    result = execute_phase_node(checkpoint, Runtime(context=context))

    assert result == checkpoint
    assert captured["expected_observation"] == GraphObservation(
        WORKFLOW_ID,
        3,
        "running",
        "review",
    )
    assert captured["repository"] is context.repository
    assert captured["context"] is context.execution


def test_real_graph_replay_fence_survives_commit_before_checkpoint_crash() -> None:
    repository = _Repository(_state(0, "running", "prepare"))
    calls: list[str] = []
    crashing_prepare = _CommitThenCrashRunner(calls)
    complete_understand = _AdvancingRunner("completed", "complete", calls)
    unused = _AdvancingRunner("completed", "complete", calls)
    context = WorkflowGraphContext(
        repository=cast(WorkflowRepository, repository),
        artifacts=cast(ArtifactStore, object()),
        execution=ExecutionContext(run_id="crash-window"),
        runners=WorkflowPhaseRunners(
            prepare=crashing_prepare,
            understand=complete_understand,
            translate_chapters=unused,
            translate_titles=unused,
            review=unused,
            quality=unused,
            export=unused,
        ),
    )
    graph = build_workflow_graph(checkpointer=InMemorySaver())
    initial = cast(
        WorkflowGraphState,
        {
            "workflow_id": WORKFLOW_ID,
            "revision": 0,
            "status": "running",
            "phase": "prepare",
        },
    )

    with pytest.raises(RuntimeError, match="after durable batch commit"):
        _invoke(graph, initial, context)

    # A None input resumes the interrupted execute task.  Its revision-zero
    # checkpoint is only a replay fence.  Revision one first hydrates read-only;
    # a newly routed prepare call then resumes from that committed batch instead
    # of repeating revision-zero work, and leaves the phase normally.
    result = graph.invoke(
        None,
        config={"configurable": {"thread_id": WORKFLOW_ID}, "recursion_limit": 1000},
        context=context,
        durability="sync",
    )

    assert result == {
        "workflow_id": WORKFLOW_ID,
        "revision": 3,
        "status": "completed",
        "phase": "complete",
    }
    assert calls == ["prepare:0", "prepare:1", "understand:crash-window"]
    config = {"configurable": {"thread_id": WORKFLOW_ID}}
    business_snapshots = [
        snapshot.values for snapshot in graph.get_state_history(config) if snapshot.values
    ]
    assert business_snapshots
    assert all(
        set(values) == {"workflow_id", "revision", "status", "phase"}
        for values in business_snapshots
    )


def test_backend_router_import_does_not_load_optional_graph_runtime() -> None:
    script = """
import sys
import trans_novel.application.backend_router

loaded = [
    name for name in sys.modules
    if name == "langgraph" or name.startswith("trans_novel.graph")
]
if loaded:
    raise SystemExit(f"unexpected optional graph imports: {loaded}")
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
