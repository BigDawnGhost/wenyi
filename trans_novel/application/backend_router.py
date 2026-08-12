"""Resolve backend ownership before constructing either execution runtime.

The router accepts factories instead of runtime instances so callers can keep
their imports inside those factories.  This is the application-level guard
that prevents a legacy task from constructing, importing, or executing the
LangGraph translation runtime by accident.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Literal, TypeAlias, TypeVar, cast

from .backend_selection import BackendName, BackendSelectionError, detect_backend

BackendPolicy: TypeAlias = Literal["auto", "legacy", "langgraph"]

_SUPPORTED_POLICIES = frozenset({"auto", "legacy", "langgraph"})
_RuntimeT = TypeVar("_RuntimeT")


class BackendPolicyConflict(BackendSelectionError):
    """Raised when an explicit policy disagrees with durable task ownership."""


def resolve_backend(
    run_dir: Path,
    *,
    policy: BackendPolicy = "auto",
    new_task_default: BackendName = "langgraph",
) -> BackendName:
    """Resolve automatic or explicit backend policy without mutating state.

    ``auto`` preserves the owner recorded in ``run_dir`` and uses
    ``new_task_default`` only for a missing or empty task.  An explicit backend
    is allowed for a new task, but it can never override an existing marker.
    This lets operators deliberately start a new legacy task for rollback while
    making implicit legacy-to-LangGraph migration impossible.
    """
    normalized_policy = _validate_policy(policy)

    # Passing an explicit policy as the new-task default distinguishes a valid
    # rollback choice from an attempt to take over state owned by another backend.
    default_backend = (
        new_task_default if normalized_policy == "auto" else cast(BackendName, normalized_policy)
    )
    detected = detect_backend(run_dir, new_task_default=default_backend)

    # Ownership mismatches stop before either runtime factory can be called.
    if normalized_policy != "auto" and detected != normalized_policy:
        raise BackendPolicyConflict(
            f"requested backend '{normalized_policy}' conflicts with "
            f"existing '{detected}' task state: {run_dir}"
        )
    return detected


def create_backend_runtime(
    run_dir: Path,
    *,
    legacy_factory: Callable[[], _RuntimeT],
    langgraph_factory: Callable[[], _RuntimeT],
    policy: BackendPolicy = "auto",
    new_task_default: BackendName = "langgraph",
) -> tuple[BackendName, _RuntimeT]:
    """Construct only the runtime that owns ``run_dir``.

    Callers should place backend-specific imports inside the supplied factories.
    Selection and conflict checks complete before a factory runs, so an ownership
    error cannot partially initialize either storage model.
    """
    backend = resolve_backend(
        run_dir,
        policy=policy,
        new_task_default=new_task_default,
    )

    # Keep the branch explicit: evaluating a mapping of pre-built runtimes here
    # would eagerly initialize both backends and violate the isolation contract.
    factory = legacy_factory if backend == "legacy" else langgraph_factory
    return backend, factory()


def _validate_policy(value: object) -> BackendPolicy:
    """Return a supported exact string policy and reject coercible lookalikes."""
    if type(value) is not str or value not in _SUPPORTED_POLICIES:
        raise ValueError("backend policy must be 'auto', 'legacy', or 'langgraph'")
    return cast(BackendPolicy, value)


__all__ = [
    "BackendPolicy",
    "BackendPolicyConflict",
    "create_backend_runtime",
    "resolve_backend",
]
