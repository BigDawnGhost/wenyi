"""Read-only backend selection from durable per-run ownership markers.

This module deliberately does not import either runtime.  Detection must happen
before a caller constructs a legacy :class:`RunStore` or a workflow repository,
because both concrete adapters may create directories as part of initialization.
"""

from __future__ import annotations

import stat
from pathlib import Path
from typing import Literal, TypeAlias, cast

BackendName: TypeAlias = Literal["legacy", "langgraph"]

_SUPPORTED_BACKENDS = frozenset({"legacy", "langgraph"})
_LEGACY_MARKER_NAMES = ("manifest.json", ".initializing.json")
_WORKFLOW_MARKER_NAME = "workflow.sqlite3"


class BackendSelectionError(RuntimeError):
    """Base error for a run directory that cannot be assigned safely."""


class BackendSelectionConflict(BackendSelectionError):
    """Raised when durable markers claim the same run for both backends."""


class UnrecognizedRunState(BackendSelectionError):
    """Raised when an existing run path has state but no ownership marker."""


def detect_backend(
    run_dir: Path,
    *,
    new_task_default: BackendName = "langgraph",
) -> BackendName:
    """Choose the owner of ``run_dir`` without creating or migrating state.

    Missing and empty directories are new tasks and use ``new_task_default``.
    A legacy manifest or interrupted-initialization marker always preserves the
    legacy owner.  ``workflow.sqlite3`` identifies the workflow backend; the
    separate LangGraph checkpoint database is intentionally not authoritative.

    Args:
        run_dir: Per-book state directory located by the application layer.
        new_task_default: Backend used only when no persistent task state exists.

    Raises:
        BackendSelectionConflict: Both legacy and workflow ownership markers exist.
        UnrecognizedRunState: The path is not an empty directory and has no marker.
        ValueError: ``new_task_default`` is not a supported backend name.
    """
    # Validate policy before observing state so a caller typo cannot silently
    # produce a different decision depending on which marker happens to exist.
    default_backend = _validate_backend_name(new_task_default)

    # Distinguish a truly absent path from an unreadable or dangling one.  Broad
    # ``Path.exists()`` checks can hide OS errors and would fail open as a new task.
    run_directory_status = _directory_status(run_dir)
    if run_directory_status == "missing":
        return default_backend

    # Files and other special nodes cannot be valid per-run directories.  Treat
    # them as unknown state instead of allowing a runtime to overwrite the path.
    if run_directory_status == "other":
        raise UnrecognizedRunState(f"run state path is not a directory: {run_dir}")

    # Inspect only explicit, durable ownership files.  We do not open either
    # database or instantiate RunStore, avoiding schema changes and migrations.
    legacy_markers = tuple(
        marker for name in _LEGACY_MARKER_NAMES if _is_regular_marker(marker := run_dir / name)
    )
    workflow_marker = run_dir / _WORKFLOW_MARKER_NAME
    has_workflow_marker = _is_regular_marker(workflow_marker)

    # Dual ownership is never resolved by precedence: choosing either backend
    # could mutate state belonging to the other, so force an explicit repair.
    if legacy_markers and has_workflow_marker:
        legacy_names = ", ".join(marker.name for marker in legacy_markers)
        raise BackendSelectionConflict(
            "run state contains both legacy and workflow markers "
            f"({legacy_names}; {workflow_marker.name}): {run_dir}"
        )

    # A legacy marker takes ownership even when initialization was interrupted;
    # this prevents the new runtime from treating partial old state as a new task.
    if legacy_markers:
        return "legacy"

    # The domain workflow database is the new backend's authoritative marker.
    # A checkpoint database alone is insufficient because it may be stale/orphaned.
    if has_workflow_marker:
        return "langgraph"

    # Only a genuinely empty existing directory is a new task.  Any unknown
    # entry fails closed so neither backend can adopt or overwrite ambiguous data.
    try:
        next(run_dir.iterdir())
    except StopIteration:
        return default_backend
    except OSError as error:
        raise UnrecognizedRunState(f"cannot inspect run state directory: {run_dir}") from error
    raise UnrecognizedRunState(
        f"run state directory is non-empty but has no recognized backend marker: {run_dir}"
    )


def _validate_backend_name(value: object) -> BackendName:
    """Return a supported default while rejecting ambiguous policy values."""
    # Exact string membership keeps booleans, enums, and typoed future backend
    # names from becoming an accidental migration policy.
    if type(value) is not str or value not in _SUPPORTED_BACKENDS:
        raise ValueError("new_task_default must be 'legacy' or 'langgraph'")
    return cast(BackendName, value)


def _directory_status(path: Path) -> Literal["directory", "missing", "other"]:
    """Classify a run path while surfacing errors that must fail closed."""
    # ``stat`` follows a usable directory link but reports a dangling link as an
    # error.  Inspecting ``lstat`` then prevents that dangling link looking absent.
    try:
        path_status = path.stat()
    except FileNotFoundError:
        try:
            path.lstat()
        except FileNotFoundError:
            return "missing"
        except OSError as error:
            raise UnrecognizedRunState(f"cannot inspect run state path: {path}") from error
        return "other"
    except OSError as error:
        raise UnrecognizedRunState(f"cannot inspect run state path: {path}") from error
    return "directory" if stat.S_ISDIR(path_status.st_mode) else "other"


def _is_regular_marker(path: Path) -> bool:
    """Recognize only accessible regular files as durable backend markers."""
    # Suppress only genuine absence.  A permissions or filesystem error leaves
    # ownership uncertain and must stop selection rather than imitate no marker.
    try:
        marker_status = path.stat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise UnrecognizedRunState(f"cannot inspect backend marker: {path}") from error
    return stat.S_ISREG(marker_status.st_mode)


__all__ = [
    "BackendName",
    "BackendSelectionConflict",
    "BackendSelectionError",
    "UnrecognizedRunState",
    "detect_backend",
]
