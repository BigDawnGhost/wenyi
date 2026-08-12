"""Owned SQLite checkpointer lifecycle for the local LangGraph runtime."""

from __future__ import annotations

import os
import sqlite3
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping, TypeAlias

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph.state import CompiledStateGraph

from ..application.runtime import ExecutionContext
from ..application.workflow_execution import WorkflowPhaseRunners, hydrate
from ..workflow.repository import ArtifactStore, WorkflowRepository
from .adapter import WorkflowGraphContext, build_workflow_graph
from .state import WorkflowGraphState, state_from_observation, validate_graph_state

CHECKPOINT_DATABASE_NAME = "langgraph-checkpoints.sqlite"
DEFAULT_RECURSION_LIMIT = 1000
_BUSINESS_DATABASE_NAME = "workflow.sqlite3"
_BUSINESS_TABLE_NAMES = frozenset({"workflow_snapshots", "workflow_operations", "workflow_outbox"})

WorkflowCompiledGraph: TypeAlias = CompiledStateGraph[
    WorkflowGraphState,
    WorkflowGraphContext,
    WorkflowGraphState,
    WorkflowGraphState,
]


@dataclass(frozen=True, slots=True)
class WorkflowGraphRuntime:
    """Compiled graph plus stable ports; execution context remains per invoke."""

    graph: WorkflowCompiledGraph
    repository: WorkflowRepository
    artifacts: ArtifactStore
    runners: WorkflowPhaseRunners

    def invoke(
        self,
        workflow_id: str,
        *,
        execution: ExecutionContext,
        recursion_limit: int = DEFAULT_RECURSION_LIMIT,
    ) -> WorkflowGraphState:
        """Start or re-enter one workflow thread from authoritative state."""
        if type(recursion_limit) is not int or recursion_limit < 1:
            raise ValueError("recursion_limit must be a positive integer")

        # Plain input intentionally restarts at START on a reused thread.  The
        # hydrate node then replaces any stale checkpoint routing observation.
        initial = state_from_observation(hydrate(workflow_id, repository=self.repository))
        context = WorkflowGraphContext(
            repository=self.repository,
            artifacts=self.artifacts,
            execution=execution,
            runners=self.runners,
        )
        result = self.graph.invoke(
            initial,
            config={
                "configurable": {"thread_id": workflow_id},
                "recursion_limit": recursion_limit,
            },
            context=context,
            durability="sync",
        )
        return _validate_graph_result(result)


@contextmanager
def open_workflow_graph_runtime(
    run_dir: str | Path,
    *,
    repository: WorkflowRepository,
    artifacts: ArtifactStore,
    runners: WorkflowPhaseRunners,
) -> Iterator[WorkflowGraphRuntime]:
    """Open the dedicated checkpoint database for exactly one runtime lifetime."""
    root = Path(run_dir)
    _require_directory(root)
    business_path = root / _BUSINESS_DATABASE_NAME
    _require_regular_file(business_path, role="workflow repository marker")

    checkpoint_path = root / CHECKPOINT_DATABASE_NAME
    if _path_lexists(checkpoint_path):
        _verify_checkpoint_isolation(business_path, checkpoint_path)

    # Strict primitive-only deserialization matches WorkflowGraphState and avoids
    # allowing arbitrary checkpoint-controlled Python classes to be constructed.
    connection = sqlite3.connect(checkpoint_path, check_same_thread=False)
    try:
        # Re-check after SQLite opens the path: an external replacement between
        # preflight and connect must not turn the saver into a business alias.
        _verify_checkpoint_isolation(business_path, checkpoint_path)
        _reject_business_schema(connection)
        saver = SqliteSaver(
            connection,
            serde=JsonPlusSerializer(
                pickle_fallback=False,
                allowed_json_modules=None,
                allowed_msgpack_modules=None,
            ),
        )
        graph = build_workflow_graph(checkpointer=saver)
        yield WorkflowGraphRuntime(
            graph=graph,
            repository=repository,
            artifacts=artifacts,
            runners=runners,
        )
    finally:
        connection.close()


def _validate_graph_result(value: object) -> WorkflowGraphState:
    """Reject framework output that leaked fields beyond the four-key contract."""
    if not isinstance(value, Mapping):
        raise RuntimeError("LangGraph returned a non-mapping workflow state projection")
    try:
        return validate_graph_state(value)
    except ValueError as error:
        raise RuntimeError("LangGraph returned an invalid workflow state projection") from error


def _require_directory(path: Path) -> None:
    """Require an existing physical run directory before creating a checkpoint."""
    try:
        path_status = path.lstat()
    except FileNotFoundError as error:
        raise ValueError(f"workflow run directory does not exist: {path}") from error
    except OSError as error:
        raise ValueError(f"cannot inspect workflow run directory: {path}") from error
    if _is_reparse_point(path_status) or not stat.S_ISDIR(path_status.st_mode):
        raise ValueError(f"workflow run path is not a physical directory: {path}")


def _require_regular_file(path: Path, *, role: str) -> None:
    """Reject missing, linked, reparse, and special ownership database paths."""
    try:
        path_status = path.lstat()
    except FileNotFoundError as error:
        raise ValueError(f"{role} does not exist: {path}") from error
    except OSError as error:
        raise ValueError(f"cannot inspect {role}: {path}") from error
    if _is_reparse_point(path_status) or not stat.S_ISREG(path_status.st_mode):
        raise ValueError(f"{role} must be a physical regular file: {path}")


def _path_lexists(path: Path) -> bool:
    """Report even dangling links so they cannot be followed by SQLite."""
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise ValueError(f"cannot inspect checkpoint database path: {path}") from error
    return True


def _is_reparse_point(path_status: os.stat_result) -> bool:
    """Recognize POSIX links and Windows reparse points from an lstat result."""
    if stat.S_ISLNK(path_status.st_mode):
        return True
    file_attributes = getattr(path_status, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(file_attributes & reparse_flag)


def _reject_business_schema(connection: sqlite3.Connection) -> None:
    """Prevent a copied business database from being adopted as a checkpoint."""
    try:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    except sqlite3.DatabaseError as error:
        raise ValueError("checkpoint path is not a readable SQLite database") from error
    collisions = sorted(str(row[0]) for row in rows if row[0] in _BUSINESS_TABLE_NAMES)
    if collisions:
        raise ValueError(
            "checkpoint database contains workflow repository tables: " + ", ".join(collisions)
        )


def _verify_checkpoint_isolation(business_path: Path, checkpoint_path: Path) -> None:
    """Revalidate the opened path and reject physical aliases to business state."""
    _require_regular_file(checkpoint_path, role="LangGraph checkpoint database")
    try:
        same_file = os.path.samefile(business_path, checkpoint_path)
    except OSError as error:
        raise ValueError("cannot verify physical checkpoint database isolation") from error
    if same_file:
        raise ValueError("graph checkpoints cannot alias the workflow repository database")


__all__ = [
    "CHECKPOINT_DATABASE_NAME",
    "DEFAULT_RECURSION_LIMIT",
    "WorkflowGraphRuntime",
    "open_workflow_graph_runtime",
]
