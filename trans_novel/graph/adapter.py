"""Real StateGraph adapter over the repository-authoritative application API."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime

from ..application.runtime import ExecutionContext
from ..application.workflow_execution import (
    WorkflowPhaseRunners,
    execute_current_phase,
    hydrate,
)
from ..domain.workflow import WorkflowStatus
from ..workflow.repository import ArtifactStore, WorkflowRepository
from .state import (
    WorkflowGraphState,
    observation_from_state,
    state_from_observation,
    validate_graph_state,
)

_ACTIVE_STATUSES = frozenset({WorkflowStatus.PENDING.value, WorkflowStatus.RUNNING.value})
_TERMINAL_STATUSES = frozenset(
    {
        WorkflowStatus.PAUSED.value,
        WorkflowStatus.COMPLETED.value,
        WorkflowStatus.FAILED.value,
    }
)


@dataclass(frozen=True, slots=True)
class WorkflowGraphContext:
    """Non-checkpointed ports and invocation-owned execution observations."""

    repository: WorkflowRepository
    artifacts: ArtifactStore
    execution: ExecutionContext
    runners: WorkflowPhaseRunners


def hydrate_node(
    state: WorkflowGraphState,
    runtime: Runtime[WorkflowGraphContext],
) -> WorkflowGraphState:
    """Replace every routing field with a fresh authoritative projection."""
    normalized = validate_graph_state(state)
    observation = hydrate(
        normalized["workflow_id"],
        repository=runtime.context.repository,
    )
    return state_from_observation(observation)


def execute_phase_node(
    state: WorkflowGraphState,
    runtime: Runtime[WorkflowGraphContext],
) -> WorkflowGraphState:
    """Execute one phase with the checkpoint projection acting only as a fence."""
    normalized = validate_graph_state(state)
    context = runtime.context
    observation = execute_current_phase(
        normalized["workflow_id"],
        repository=context.repository,
        artifacts=context.artifacts,
        context=context.execution,
        runners=context.runners,
        expected_observation=observation_from_state(normalized),
    )
    return state_from_observation(observation)


def route_workflow(state: WorkflowGraphState) -> Literal["execute_phase", "__end__"]:
    """Continue active work sequentially and terminate every inert lifecycle."""
    status = validate_graph_state(state)["status"]
    if status in _ACTIVE_STATUSES:
        return "execute_phase"
    if status in _TERMINAL_STATUSES:
        return END
    raise ValueError(f"unsupported workflow status in graph state: {status!r}")


def build_workflow_graph(
    *,
    checkpointer: BaseCheckpointSaver[str],
) -> CompiledStateGraph[
    WorkflowGraphState,
    WorkflowGraphContext,
    WorkflowGraphState,
    WorkflowGraphState,
]:
    """Compile the stable two-node workflow topology with an external saver."""
    # One generic execution node keeps node names stable as phase services evolve;
    # WorkflowPhaseRunners remains the sole phase-to-service dispatch table.
    builder = StateGraph(WorkflowGraphState, context_schema=WorkflowGraphContext)
    builder.add_node("hydrate", hydrate_node)
    builder.add_node("execute_phase", execute_phase_node)
    builder.add_edge(START, "hydrate")
    builder.add_conditional_edges("hydrate", route_workflow)
    builder.add_conditional_edges("execute_phase", route_workflow)
    return builder.compile(checkpointer=checkpointer, name="wenyi-workflow")


__all__ = [
    "WorkflowGraphContext",
    "build_workflow_graph",
    "execute_phase_node",
    "hydrate_node",
    "route_workflow",
]
