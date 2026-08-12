"""Lazy backend routing tests that prove unselected runtimes stay untouched."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from trans_novel.application.backend_router import (
    BackendPolicyConflict,
    create_backend_runtime,
    resolve_backend,
)


class _FactoryProbe:
    """Record construction so tests can detect accidental eager initialization."""

    def __init__(self, name: str, *, fail: bool = False) -> None:
        self.name = name
        self.fail = fail
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        if self.fail:
            raise AssertionError(f"unselected {self.name} factory was called")
        return f"{self.name}-runtime"


def test_legacy_marker_constructs_only_legacy_runtime(tmp_path: Path) -> None:
    run_dir = tmp_path / "book"
    run_dir.mkdir()
    (run_dir / "manifest.json").write_text("{}", encoding="utf-8")
    legacy = _FactoryProbe("legacy")
    langgraph = _FactoryProbe("langgraph", fail=True)

    selected, runtime = create_backend_runtime(
        run_dir,
        legacy_factory=legacy,
        langgraph_factory=langgraph,
    )

    assert (selected, runtime) == ("legacy", "legacy-runtime")
    assert (legacy.calls, langgraph.calls) == (1, 0)


def test_workflow_marker_constructs_only_langgraph_runtime(tmp_path: Path) -> None:
    run_dir = tmp_path / "book"
    run_dir.mkdir()
    (run_dir / "workflow.sqlite3").write_bytes(b"opaque marker")
    legacy = _FactoryProbe("legacy", fail=True)
    langgraph = _FactoryProbe("langgraph")

    selected, runtime = create_backend_runtime(
        run_dir,
        legacy_factory=legacy,
        langgraph_factory=langgraph,
    )

    assert (selected, runtime) == ("langgraph", "langgraph-runtime")
    assert (legacy.calls, langgraph.calls) == (0, 1)


def test_new_task_defaults_to_langgraph_but_allows_explicit_legacy(tmp_path: Path) -> None:
    automatic_dir = tmp_path / "automatic"
    rollback_dir = tmp_path / "rollback"

    assert resolve_backend(automatic_dir) == "langgraph"
    assert resolve_backend(rollback_dir, policy="legacy") == "legacy"
    assert not automatic_dir.exists()
    assert not rollback_dir.exists()


@pytest.mark.parametrize(
    ("marker", "policy", "owner"),
    [
        ("manifest.json", "langgraph", "legacy"),
        ("workflow.sqlite3", "legacy", "langgraph"),
    ],
)
def test_explicit_policy_cannot_take_over_existing_state(
    tmp_path: Path,
    marker: str,
    policy: str,
    owner: str,
) -> None:
    run_dir = tmp_path / "book"
    run_dir.mkdir()
    (run_dir / marker).touch()
    legacy = _FactoryProbe("legacy", fail=True)
    langgraph = _FactoryProbe("langgraph", fail=True)

    with pytest.raises(BackendPolicyConflict, match=f"existing '{owner}'"):
        create_backend_runtime(
            run_dir,
            legacy_factory=legacy,
            langgraph_factory=langgraph,
            policy=policy,  # type: ignore[arg-type]
        )

    assert (legacy.calls, langgraph.calls) == (0, 0)


@pytest.mark.parametrize("policy", ["", "AUTO", "new", None, True])
def test_invalid_policy_stops_before_state_or_factories(
    tmp_path: Path,
    policy: object,
) -> None:
    run_dir = tmp_path / "missing"
    legacy = _FactoryProbe("legacy", fail=True)
    langgraph = _FactoryProbe("langgraph", fail=True)

    with pytest.raises(ValueError, match="backend policy"):
        create_backend_runtime(
            run_dir,
            legacy_factory=legacy,
            langgraph_factory=langgraph,
            policy=policy,  # type: ignore[arg-type]
        )

    assert not run_dir.exists()
    assert (legacy.calls, langgraph.calls) == (0, 0)


def test_selected_runtime_failure_does_not_fall_back_to_other_backend(tmp_path: Path) -> None:
    legacy = _FactoryProbe("legacy", fail=True)
    langgraph = _FactoryProbe("langgraph")

    with pytest.raises(AssertionError, match="unselected legacy"):
        create_backend_runtime(
            tmp_path / "new",
            legacy_factory=legacy,
            langgraph_factory=langgraph,
            policy="legacy",
        )

    assert (legacy.calls, langgraph.calls) == (1, 0)


def test_clean_import_does_not_load_either_backend() -> None:
    code = """
import sys
import trans_novel.application.backend_router
forbidden = (
    'trans_novel.pipeline.orchestrator',
    'trans_novel.pipeline.runstore',
    'trans_novel.storage.sqlite_workflows',
    'langgraph',
)
loaded = [name for name in forbidden if name in sys.modules]
if loaded:
    raise SystemExit(','.join(loaded))
"""

    completed = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
