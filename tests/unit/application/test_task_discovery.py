"""Read-only legacy task discovery tests."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from trans_novel.application.task_discovery import (
    InvalidLegacyTaskMarker,
    LegacyTaskDiscoveryConflict,
    LegacyTaskDiscoveryError,
    discover_legacy_run_dir,
)

SOURCE_DIGEST = "a" * 64
OTHER_DIGEST = "b" * 64


@pytest.mark.parametrize("create_empty_root", [False, True])
def test_missing_or_empty_state_root_has_no_legacy_task(
    tmp_path: Path,
    create_empty_root: bool,
) -> None:
    state_root = tmp_path / "state"
    if create_empty_root:
        state_root.mkdir()

    assert discover_legacy_run_dir(state_root, SOURCE_DIGEST) is None
    assert state_root.exists() is create_empty_root
    if create_empty_root:
        assert list(state_root.iterdir()) == []


@pytest.mark.parametrize("marker_name", ["manifest.json", ".initializing.json"])
def test_each_legacy_owner_marker_locates_its_direct_run(
    tmp_path: Path,
    marker_name: str,
) -> None:
    state_root = tmp_path / "state"
    run_dir = state_root / "book"
    run_dir.mkdir(parents=True)
    _write_marker(run_dir / marker_name, SOURCE_DIGEST)
    before = _tree_snapshot(state_root)

    assert discover_legacy_run_dir(state_root, SOURCE_DIGEST) == run_dir
    assert _tree_snapshot(state_root) == before


def test_matching_nested_run_is_not_scanned(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    nested_run = state_root / "group" / "book"
    nested_run.mkdir(parents=True)
    _write_marker(nested_run / "manifest.json", SOURCE_DIGEST)

    assert discover_legacy_run_dir(state_root, SOURCE_DIGEST) is None


def test_unknown_direct_entries_are_ignored(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    unknown_dir = state_root / "cache"
    unknown_dir.mkdir(parents=True)
    (unknown_dir / "metadata.json").write_text("not-json", encoding="utf-8")
    (state_root / "README.txt").write_text("state notes", encoding="utf-8")

    assert discover_legacy_run_dir(state_root, SOURCE_DIGEST) is None


def test_linked_direct_child_fails_closed(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    target = tmp_path / "external-legacy-run"
    target.mkdir()
    _write_marker(target / "manifest.json", SOURCE_DIGEST)
    linked_run = state_root / "linked-book"
    try:
        linked_run.symlink_to(target, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks are unavailable on this platform: {error}")

    with pytest.raises(LegacyTaskDiscoveryError, match="link or reparse point"):
        discover_legacy_run_dir(state_root, SOURCE_DIGEST)


def test_valid_nonmatching_legacy_task_is_not_returned(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    run_dir = state_root / "other-book"
    run_dir.mkdir(parents=True)
    _write_marker(run_dir / "manifest.json", OTHER_DIGEST)

    assert discover_legacy_run_dir(state_root, SOURCE_DIGEST) is None


def test_manifest_and_initializing_marker_may_share_one_owner(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    run_dir = state_root / "book"
    run_dir.mkdir(parents=True)
    _write_marker(run_dir / "manifest.json", SOURCE_DIGEST)
    _write_marker(run_dir / ".initializing.json", SOURCE_DIGEST)

    assert discover_legacy_run_dir(state_root, SOURCE_DIGEST) == run_dir


def test_disagreeing_markers_fail_even_when_neither_matches_request(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    run_dir = state_root / "book"
    run_dir.mkdir(parents=True)
    _write_marker(run_dir / "manifest.json", "c" * 64)
    _write_marker(run_dir / ".initializing.json", "d" * 64)

    with pytest.raises(LegacyTaskDiscoveryConflict, match="markers disagree"):
        discover_legacy_run_dir(state_root, SOURCE_DIGEST)


def test_duplicate_matching_owners_are_ambiguous(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    first = state_root / "first"
    second = state_root / "second"
    first.mkdir(parents=True)
    second.mkdir()
    _write_marker(first / "manifest.json", SOURCE_DIGEST)
    _write_marker(second / ".initializing.json", SOURCE_DIGEST)

    with pytest.raises(LegacyTaskDiscoveryConflict, match="multiple legacy run directories"):
        discover_legacy_run_dir(state_root, SOURCE_DIGEST)


@pytest.mark.parametrize(
    "marker_payload",
    [
        b"not-json",
        b"[]",
        b"{}",
        b'{"source_sha256": true}',
        b'{"source_sha256": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"}',
        b'{"source_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        b' "source_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}',
        b"\xff",
    ],
)
def test_malformed_marker_fails_closed_even_when_it_cannot_match(
    tmp_path: Path,
    marker_payload: bytes,
) -> None:
    state_root = tmp_path / "state"
    run_dir = state_root / "broken"
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_bytes(marker_payload)

    with pytest.raises(InvalidLegacyTaskMarker, match="legacy owner marker"):
        discover_legacy_run_dir(state_root, OTHER_DIGEST)


def test_marker_named_directory_is_rejected(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    marker = state_root / "book" / "manifest.json"
    marker.mkdir(parents=True)

    with pytest.raises(InvalidLegacyTaskMarker, match="not a regular file"):
        discover_legacy_run_dir(state_root, SOURCE_DIGEST)


@pytest.mark.parametrize("invalid_digest", ["", "a" * 63, "A" * 64, None, True, 1])
def test_requested_digest_must_already_be_canonical(
    tmp_path: Path,
    invalid_digest: object,
) -> None:
    state_root = tmp_path / "missing"

    with pytest.raises(ValueError, match="source_sha256"):
        discover_legacy_run_dir(state_root, invalid_digest)  # type: ignore[arg-type]

    assert not state_root.exists()


def test_non_directory_state_root_is_rejected(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.write_text("occupied", encoding="utf-8")

    with pytest.raises(LegacyTaskDiscoveryError, match="not a directory"):
        discover_legacy_run_dir(state_root, SOURCE_DIGEST)


def test_marker_permission_error_is_explicit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    marker = state_root / "book" / "manifest.json"
    marker.parent.mkdir(parents=True)
    _write_marker(marker, SOURCE_DIGEST)
    original_lstat = Path.lstat

    def guarded_lstat(path: Path):  # type: ignore[no-untyped-def]
        if path == marker:
            raise PermissionError("denied")
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", guarded_lstat)

    with pytest.raises(InvalidLegacyTaskMarker, match="cannot inspect"):
        discover_legacy_run_dir(state_root, SOURCE_DIGEST)


def test_clean_import_does_not_load_either_runtime() -> None:
    project_root = Path(__file__).resolve().parents[3]
    script = """
import sys
sys.path.insert(0, sys.argv[1])
import trans_novel.application.task_discovery

forbidden = (
    "trans_novel.pipeline",
    "trans_novel.ingest",
    "trans_novel.storage",
    "trans_novel.graph",
    "langgraph",
)
loaded = sorted(
    name
    for name in sys.modules
    if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
)
if loaded:
    raise SystemExit("unexpected runtime imports: " + ", ".join(loaded))
"""

    # ``-I`` proves isolation from the current interpreter and ambient Python
    # path; the repository root is inserted explicitly for the module under test.
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script, str(project_root)],
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout


def _write_marker(path: Path, digest: str) -> None:
    """Write the common owner field shared by both legacy marker shapes."""
    path.write_text(json.dumps({"source_sha256": digest}), encoding="utf-8")


def _tree_snapshot(root: Path) -> tuple[tuple[str, bytes | None], ...]:
    """Capture the tree to prove discovery performs no writes."""
    return tuple(
        (
            path.relative_to(root).as_posix(),
            None if path.is_dir() else path.read_bytes(),
        )
        for path in sorted(root.rglob("*"))
    )
