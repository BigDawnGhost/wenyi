"""Backend ownership detection tests with mutation guards."""

from __future__ import annotations

from pathlib import Path

import pytest

from trans_novel.application.backend_selection import (
    BackendSelectionConflict,
    UnrecognizedRunState,
    detect_backend,
)


@pytest.mark.parametrize("create_empty_directory", [False, True])
def test_missing_or_empty_run_is_a_new_langgraph_task(
    tmp_path: Path,
    create_empty_directory: bool,
) -> None:
    run_dir = tmp_path / "book"
    if create_empty_directory:
        run_dir.mkdir()

    assert detect_backend(run_dir) == "langgraph"
    assert run_dir.exists() is create_empty_directory
    if create_empty_directory:
        assert list(run_dir.iterdir()) == []


@pytest.mark.parametrize("create_empty_directory", [False, True])
def test_new_task_default_is_used_only_without_persistent_state(
    tmp_path: Path,
    create_empty_directory: bool,
) -> None:
    run_dir = tmp_path / "book"
    if create_empty_directory:
        run_dir.mkdir()

    assert detect_backend(run_dir, new_task_default="legacy") == "legacy"


@pytest.mark.parametrize("marker_name", ["manifest.json", ".initializing.json"])
def test_each_legacy_runstore_marker_preserves_legacy_ownership(
    tmp_path: Path,
    marker_name: str,
) -> None:
    run_dir = tmp_path / "book"
    run_dir.mkdir()
    marker = run_dir / marker_name
    marker.write_text("legacy-state", encoding="utf-8")
    unrelated = run_dir / "chapters"
    unrelated.mkdir()
    before = _tree_snapshot(run_dir)

    assert detect_backend(run_dir) == "legacy"
    assert _tree_snapshot(run_dir) == before


def test_domain_workflow_database_marks_langgraph_backend(tmp_path: Path) -> None:
    run_dir = tmp_path / "book"
    run_dir.mkdir()
    workflow_marker = run_dir / "workflow.sqlite3"
    workflow_marker.write_bytes(b"not opened by detection")
    (run_dir / "langgraph-checkpoints.sqlite").write_bytes(b"checkpoint")
    before = _tree_snapshot(run_dir)

    assert detect_backend(run_dir, new_task_default="legacy") == "langgraph"
    assert _tree_snapshot(run_dir) == before


@pytest.mark.parametrize("legacy_marker", ["manifest.json", ".initializing.json"])
def test_dual_backend_markers_fail_closed(
    tmp_path: Path,
    legacy_marker: str,
) -> None:
    run_dir = tmp_path / "book"
    run_dir.mkdir()
    (run_dir / legacy_marker).touch()
    (run_dir / "workflow.sqlite3").touch()
    before = _tree_snapshot(run_dir)

    with pytest.raises(BackendSelectionConflict, match="both legacy and workflow"):
        detect_backend(run_dir)

    assert _tree_snapshot(run_dir) == before


@pytest.mark.parametrize(
    "unknown_entry",
    ["langgraph-checkpoints.sqlite", "events.jsonl", "workflow.sqlite3-wal"],
)
def test_nonempty_directory_without_authoritative_marker_is_rejected(
    tmp_path: Path,
    unknown_entry: str,
) -> None:
    run_dir = tmp_path / "book"
    run_dir.mkdir()
    (run_dir / unknown_entry).touch()

    with pytest.raises(UnrecognizedRunState, match="non-empty"):
        detect_backend(run_dir)


@pytest.mark.parametrize("marker_name", ["manifest.json", ".initializing.json", "workflow.sqlite3"])
def test_marker_named_directory_does_not_claim_backend(
    tmp_path: Path,
    marker_name: str,
) -> None:
    run_dir = tmp_path / "book"
    run_dir.mkdir()
    (run_dir / marker_name).mkdir()

    with pytest.raises(UnrecognizedRunState, match="non-empty"):
        detect_backend(run_dir)


def test_non_directory_run_path_is_rejected(tmp_path: Path) -> None:
    run_path = tmp_path / "book"
    run_path.write_text("occupied", encoding="utf-8")

    with pytest.raises(UnrecognizedRunState, match="not a directory"):
        detect_backend(run_path)


@pytest.mark.parametrize("invalid_default", ["", "auto", None, True, 1])
def test_invalid_new_task_default_is_rejected(
    tmp_path: Path,
    invalid_default: object,
) -> None:
    run_dir = tmp_path / "missing"

    with pytest.raises(ValueError, match="new_task_default"):
        detect_backend(run_dir, new_task_default=invalid_default)  # type: ignore[arg-type]

    assert not run_dir.exists()


def _tree_snapshot(root: Path) -> tuple[tuple[str, bool, bytes | None], ...]:
    """Capture names, node kinds, and file bytes to prove detection is read-only."""
    return tuple(
        (
            path.relative_to(root).as_posix(),
            path.is_dir(),
            None if path.is_dir() else path.read_bytes(),
        )
        for path in sorted(root.rglob("*"))
    )
