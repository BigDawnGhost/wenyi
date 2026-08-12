"""Framework-neutral execution boundary for one durable workflow phase.

The workflow repository is the sole authority for business state.  A graph,
scheduler, CLI, or test harness may call this module, but none of them may pass
an in-memory snapshot forward as if it were a committed result.  Phase runners
publish immutable artifacts and commit their own state patches; this boundary
then reloads the authoritative snapshot before returning a small observation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..domain.workflow import WorkflowPhase, WorkflowStatus
from ..workflow.repository import ArtifactStore, WorkflowRepository
from ..workflow.state import WorkflowState
from .runtime import ExecutionContext

_NON_EXECUTABLE_STATUSES = frozenset(
    {
        WorkflowStatus.PAUSED.value,
        WorkflowStatus.COMPLETED.value,
        WorkflowStatus.FAILED.value,
    }
)
_ACTIVE_STATUSES = frozenset(
    {
        WorkflowStatus.PENDING.value,
        WorkflowStatus.RUNNING.value,
    }
)


class WorkflowDidNotProgress(RuntimeError):
    """Raised when a runner returns without durably finishing or stopping its phase."""


class UnsupportedWorkflowPhase(RuntimeError):
    """Raised when an active workflow has no registered phase runner."""


class WorkflowObservationConflict(RuntimeError):
    """Raised when a replay expectation contradicts authoritative workflow state."""


@dataclass(frozen=True, slots=True)
class GraphObservation:
    """Small serializable projection safe to hand to an execution framework.

    The projection intentionally excludes stage payloads, artifact bodies, and
    runtime clients.  It is an observation of committed state, not a second
    mutable workflow state owned by a graph checkpoint.
    """

    workflow_id: str
    revision: int
    status: str
    phase: str


class WorkflowPhaseRunner(Protocol):
    """Application service that durably executes one currently selected phase.

    A runner receives the detached snapshot used for dispatch and the narrow
    ports needed to publish artifacts, commit compare-and-swap patches, and
    report invocation-scoped observations.  It may commit multiple internal
    batches, but before returning it must leave the selected phase, pause/fail,
    or complete the workflow.  It returns ``None``; mutating ``state`` alone
    has no effect.
    """

    def __call__(
        self,
        state: WorkflowState,
        *,
        repository: WorkflowRepository,
        artifacts: ArtifactStore,
        context: ExecutionContext,
    ) -> None:
        """Durably finish or stop the phase selected by ``state.cursor.phase``."""
        ...


@dataclass(frozen=True, slots=True)
class WorkflowPhaseRunners:
    """Immutable registry containing exactly one runner per executable phase."""

    prepare: WorkflowPhaseRunner
    understand: WorkflowPhaseRunner
    translate_chapters: WorkflowPhaseRunner
    translate_titles: WorkflowPhaseRunner
    review: WorkflowPhaseRunner
    quality: WorkflowPhaseRunner
    export: WorkflowPhaseRunner

    def for_phase(self, phase: str) -> WorkflowPhaseRunner:
        """Return the runner for a persisted phase without importing a runtime."""
        # An explicit branch table keeps COMPLETE deliberately non-executable
        # and makes additions to WorkflowPhase visible during code review.
        if phase == WorkflowPhase.PREPARE.value:
            return self.prepare
        if phase == WorkflowPhase.UNDERSTAND.value:
            return self.understand
        if phase == WorkflowPhase.TRANSLATE_CHAPTERS.value:
            return self.translate_chapters
        if phase == WorkflowPhase.TRANSLATE_TITLES.value:
            return self.translate_titles
        if phase == WorkflowPhase.REVIEW.value:
            return self.review
        if phase == WorkflowPhase.QUALITY.value:
            return self.quality
        if phase == WorkflowPhase.EXPORT.value:
            return self.export
        raise UnsupportedWorkflowPhase(f"active workflow has no runner for phase {phase!r}")


def hydrate(
    workflow_id: str,
    *,
    repository: WorkflowRepository,
) -> GraphObservation:
    """Reload and project the authoritative workflow snapshot."""
    # Never accept a caller-provided state here: every graph entry or resume
    # must observe the repository revision that survived the previous process.
    return _observe(repository.get(workflow_id))


def execute_current_phase(
    workflow_id: str,
    *,
    repository: WorkflowRepository,
    artifacts: ArtifactStore,
    context: ExecutionContext,
    runners: WorkflowPhaseRunners,
    expected_observation: GraphObservation | None = None,
) -> GraphObservation:
    """Execute at most one active phase and return freshly committed progress.

    Paused, failed, and completed workflows are observation-only.  Pending and
    running workflows dispatch from their persisted cursor, allowing the
    preparation runner to perform the initial pending-to-running transition.
    After the runner returns, the repository is read again.  Every active runner
    must commit at least one patch and leave its selected phase unless it paused
    or failed.  This keeps batch loops inside the application service instead of
    turning each batch into a LangGraph recursion step.
    """
    # The first load owns dispatch.  A checkpoint observation may fence replay,
    # but it never replaces the repository snapshot used to select the runner.
    state = repository.get(workflow_id)
    before = _observe(state)
    if expected_observation is not None:
        replay_result = _check_replay_expectation(before, expected_observation)
        if replay_result is not None:
            return replay_result
    if before.status in _NON_EXECUTABLE_STATUSES:
        return before

    runner = runners.for_phase(before.phase)
    runner(
        state,
        repository=repository,
        artifacts=artifacts,
        context=context,
    )

    # The runner's local snapshot may have been mutated or become stale.  Only
    # the second repository read can describe the durable result to a graph.
    after = _observe(repository.get(workflow_id))
    if after.revision <= before.revision:
        raise WorkflowDidNotProgress(
            f"workflow {workflow_id!r} did not advance active phase "
            f"{before.phase!r} beyond revision {before.revision}"
        )
    if after.status in _ACTIVE_STATUSES and after.phase == before.phase:
        raise WorkflowDidNotProgress(
            f"workflow {workflow_id!r} remained active in phase {before.phase!r} "
            f"after advancing to revision {after.revision}"
        )
    if (
        after.status == WorkflowStatus.COMPLETED.value
        and after.phase != WorkflowPhase.COMPLETE.value
    ):
        raise WorkflowDidNotProgress(
            f"workflow {workflow_id!r} completed without entering phase "
            f"{WorkflowPhase.COMPLETE.value!r}"
        )
    return after


def _check_replay_expectation(
    actual: GraphObservation,
    expected: GraphObservation,
) -> GraphObservation | None:
    """Fence node replay against a stale or contradictory graph checkpoint.

    A newer repository revision proves the prior node attempt committed before
    its checkpoint write, so replay becomes a read-only hydration.  An older
    repository or different routing fields at the same revision cannot arise
    from the repository contract and therefore fails closed.
    """
    if actual.workflow_id != expected.workflow_id:
        raise WorkflowObservationConflict(
            "expected observation belongs to a different workflow: "
            f"{expected.workflow_id!r} != {actual.workflow_id!r}"
        )
    if actual.revision > expected.revision:
        return actual
    if actual.revision < expected.revision:
        raise WorkflowObservationConflict(
            f"workflow {actual.workflow_id!r} repository revision {actual.revision} "
            f"is behind expected revision {expected.revision}"
        )
    if actual.status != expected.status or actual.phase != expected.phase:
        raise WorkflowObservationConflict(
            f"workflow {actual.workflow_id!r} routing changed without a revision advance"
        )
    return None


def _observe(state: WorkflowState) -> GraphObservation:
    """Detach the four stable routing fields from a complete workflow state."""
    return GraphObservation(
        workflow_id=state["workflow_id"],
        revision=state["revision"],
        status=state["status"],
        phase=state["cursor"]["phase"],
    )


__all__ = [
    "GraphObservation",
    "UnsupportedWorkflowPhase",
    "WorkflowDidNotProgress",
    "WorkflowObservationConflict",
    "WorkflowPhaseRunner",
    "WorkflowPhaseRunners",
    "execute_current_phase",
    "hydrate",
]
