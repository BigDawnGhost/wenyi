"""Minimal LangGraph checkpoint state for durable workflow routing."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TypedDict

from ..application.workflow_execution import GraphObservation
from ..domain.workflow import WorkflowPhase, WorkflowStatus

_WORKFLOW_ID_PATTERN = re.compile(r"\Awf-[0-9a-f]{64}\Z")
_GRAPH_STATE_KEYS = frozenset({"workflow_id", "revision", "status", "phase"})
_WORKFLOW_STATUSES = frozenset(status.value for status in WorkflowStatus)
_WORKFLOW_PHASES = frozenset(phase.value for phase in WorkflowPhase)


class WorkflowGraphState(TypedDict):
    """Only the durable workflow identity and its latest routing observation."""

    workflow_id: str
    revision: int
    status: str
    phase: str


def state_from_observation(observation: GraphObservation) -> WorkflowGraphState:
    """Copy an application observation into primitive checkpoint fields."""
    return validate_graph_state(
        {
            "workflow_id": observation.workflow_id,
            "revision": observation.revision,
            "status": observation.status,
            "phase": observation.phase,
        }
    )


def observation_from_state(state: WorkflowGraphState) -> GraphObservation:
    """Copy primitive checkpoint fields into the application replay fence."""
    normalized = validate_graph_state(state)
    return GraphObservation(
        workflow_id=normalized["workflow_id"],
        revision=normalized["revision"],
        status=normalized["status"],
        phase=normalized["phase"],
    )


def validate_graph_state(value: Mapping[str, object]) -> WorkflowGraphState:
    """Validate an exact primitive checkpoint projection and return a copy."""
    if set(value) != _GRAPH_STATE_KEYS:
        raise ValueError("workflow graph state must contain exactly four routing fields")
    workflow_id = value["workflow_id"]
    revision = value["revision"]
    status = value["status"]
    phase = value["phase"]
    if type(workflow_id) is not str or _WORKFLOW_ID_PATTERN.fullmatch(workflow_id) is None:
        raise ValueError(
            "workflow graph workflow_id must be wf- followed by 64 lowercase hex digits"
        )
    if type(revision) is not int or revision < 0:
        raise ValueError("workflow graph revision must be a non-negative native integer")
    if type(status) is not str or status not in _WORKFLOW_STATUSES:
        raise ValueError("workflow graph status is unsupported")
    if type(phase) is not str or phase not in _WORKFLOW_PHASES:
        raise ValueError("workflow graph phase is unsupported")
    return {
        "workflow_id": workflow_id,
        "revision": revision,
        "status": status,
        "phase": phase,
    }


__all__ = [
    "WorkflowGraphState",
    "observation_from_state",
    "state_from_observation",
    "validate_graph_state",
]
