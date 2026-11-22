"""Old-format Desktop data must be detected and explained, never migrated."""

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from wenyi_desktop import catalog, paths
from wenyi_desktop.local_backend import LocalBackend
from wenyi_desktop.main import create_app, create_context, legacy_data_report


def _populate_legacy_catalog(root: Path) -> None:
    connection = sqlite3.connect(root / "catalog.sqlite3")
    try:
        connection.execute("CREATE TABLE projects (id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO projects VALUES ('legacy')")
        connection.commit()
    finally:
        connection.close()


def _failure_payload(stdout: str) -> dict:
    assert stdout.startswith("WENYI_FAILED ")
    return json.loads(stdout.removeprefix("WENYI_FAILED ").strip())


def test_legacy_catalog_refusal_carries_import_guidance(tmp_path, capsys):
    root = tmp_path / "data"
    root.mkdir()
    _populate_legacy_catalog(root)
    before = (root / "catalog.sqlite3").read_bytes()

    with pytest.raises(RuntimeError, match="current-format Desktop catalog"):
        create_context(root)

    payload = _failure_payload(capsys.readouterr().out)
    assert payload["reason"] == "migration"
    assert payload["restored"] is False
    assert payload["legacy_data"] is True
    assert any("Import project" in step for step in payload["guidance"])
    assert any("project.json" in step for step in payload["guidance"])
    assert "does not migrate" in payload["guidance"][0]
    assert (root / "catalog.sqlite3").read_bytes() == before


def test_other_failures_keep_the_original_payload(tmp_path, capsys, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()

    def fail(self, *args, **kwargs):
        raise RuntimeError("unrelated boom")

    monkeypatch.setattr(LocalBackend, "__init__", fail)
    with pytest.raises(RuntimeError, match="unrelated boom"):
        create_context(root)

    payload = _failure_payload(capsys.readouterr().out)
    assert "legacy_data" not in payload
    assert "guidance" not in payload


def test_old_format_folders_are_reported_and_left_untouched(tmp_path, capsys):
    root = tmp_path / "data"
    legacy_project = root / "projects" / "0123456789abcdef"
    legacy_project.mkdir(parents=True)
    (legacy_project / "state.sqlite3").write_bytes(b"legacy-state")
    (root / "local.sqlite3").write_bytes(b"legacy-root")
    before = (legacy_project / "state.sqlite3").read_bytes()

    with TestClient(create_app(root)) as client:
        assert client.get("/projects").json() == []

    captured = capsys.readouterr()
    assert "does not migrate" in captured.err
    assert "Import project" in captured.err
    assert (legacy_project / "state.sqlite3").read_bytes() == before
    assert (root / "local.sqlite3").read_bytes() == b"legacy-root"


def test_current_format_startup_prints_no_legacy_guidance(tmp_path, capsys):
    root = tmp_path / "data"
    root.mkdir()
    with TestClient(create_app(root)) as client:
        assert client.get("/projects").json() == []
    assert "Old-format Wenyi Desktop data" not in capsys.readouterr().err


def test_legacy_data_report_classifies_folders(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    assert legacy_data_report(root) == {
        "legacy_catalog": False,
        "legacy_database": False,
        "old_project_folders": [],
    }

    legacy = root / "projects" / "aaaaaaaaaaaaaaaa"
    legacy.mkdir(parents=True)
    (legacy / "state.sqlite3").write_bytes(b"old")
    marked = root / "projects" / "bbbbbbbbbbbbbbbb"
    marked.mkdir()
    (marked / paths.PROJECT_MARKER).write_text(
        json.dumps({"schema_version": paths.PROJECT_SCHEMA_VERSION}), encoding="utf-8"
    )
    outdated = root / "projects" / "cccccccccccccccc"
    outdated.mkdir()
    (outdated / paths.PROJECT_MARKER).write_text(
        json.dumps({"schema_version": 1}), encoding="utf-8"
    )
    deleting = root / "projects" / "dddddddddddddddd"
    deleting.mkdir()
    (deleting / paths.PROJECT_DELETING).write_text("", encoding="utf-8")
    (deleting / "state.sqlite3").write_bytes(b"old")
    (root / "projects" / "eeeeeeeeeeeeeeee").mkdir()
    (root / "local.sqlite3").write_bytes(b"old")

    report = legacy_data_report(root)
    assert report["legacy_database"] is True
    assert report["old_project_folders"] == ["aaaaaaaaaaaaaaaa", "cccccccccccccccc"]


def test_legacy_data_report_classifies_catalogs(tmp_path):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    _populate_legacy_catalog(legacy)
    assert legacy_data_report(legacy)["legacy_catalog"] is True

    current = tmp_path / "current"
    current.mkdir()
    connection = sqlite3.connect(current / "catalog.sqlite3")
    try:
        connection.execute("CREATE TABLE projects (id TEXT PRIMARY KEY)")
        connection.execute(f"PRAGMA application_id = {catalog.APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version = {catalog.FORMAT_VERSION}")
        connection.commit()
    finally:
        connection.close()
    assert legacy_data_report(current)["legacy_catalog"] is False

    empty = tmp_path / "empty"
    empty.mkdir()
    sqlite3.connect(empty / "catalog.sqlite3").close()
    assert legacy_data_report(empty)["legacy_catalog"] is False

    corrupt = tmp_path / "corrupt"
    corrupt.mkdir()
    (corrupt / "catalog.sqlite3").write_bytes(b"this is not a database")
    assert legacy_data_report(corrupt)["legacy_catalog"] is True
