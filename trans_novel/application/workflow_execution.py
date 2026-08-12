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


class WorkflowDidNotProgress(RuntimeError):
    """Raised when an active phase returns without a durable state advance."""


class UnsupportedWorkflowPhase(RuntimeError):
    """Raised when an active workflow has no registered phase runner."""


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
    ports needed to publish artifacts, commit a compare-and-swap patch, and
    report invocation-scoped observations.  It must return ``None`` and commit
    progress through ``repository``; mutating ``state`` alone has no effect.
    """

    def __call__(
        self,
        state: WorkflowState,
        *,
        repository: WorkflowRepository,
        artifacts: ArtifactStore,
        context: ExecutionContext,
    ) -> None:
        """Execute and commit the phase selected by ``state.cursor.phase``."""
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
) -> GraphObservation:
    """Execute at most one active phase and return freshly committed progress.

    Paused, failed, and completed workflows are observation-only.  Pending and
    running workflows dispatch from their persisted cursor, allowing the
    preparation runner to perform the initial pending-to-running transition.
    After the runner returns, the repository is read again.  Every active runner
    must commit at least one patch, so a revision that did not strictly increase
    is rejected before a graph can spin around a no-op service.
    """
    # The first load owns dispatch.  A checkpoint observation is only a wake-up
    # hint and therefore never participates in selecting the phase runner.
    state = repository.get(workflow_id)
    before = _observe(state)
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
    return after


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
    "WorkflowPhaseRunner",
    "WorkflowPhaseRunners",
    "execute_current_phase",
    "hydrate",
]
