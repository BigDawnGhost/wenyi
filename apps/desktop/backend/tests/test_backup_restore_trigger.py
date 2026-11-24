"""Regression tests for when a failed startup may restore the pre-update backup."""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest
from wenyi_core.errors import SchemaTooNew
from wenyi_desktop import main


def _restore_recorder(calls):
    def restore(workspace):
        calls.append(workspace)
        return SimpleNamespace(status="restored", detail="restored snapshots")

    return restore


@pytest.mark.parametrize(
    "error",
    [
        sqlite3.OperationalError("file is not a database"),
        RuntimeError("another engine instance is already running"),
        ValueError("configuration file is not valid YAML"),
    ],
    ids=["corrupt-database", "engine-lock", "bad-config"],
)
def test_unrelated_startup_failure_keeps_live_data(monkeypatch, tmp_path, capsys, error):
    calls = []

    def failing_backend(*args, **kwargs):
        raise error

    monkeypatch.setattr(main, "LocalBackend", failing_backend)
    monkeypatch.setattr(main, "restore_latest_backup", _restore_recorder(calls))
    monkeypatch.chdir(tmp_path)

    with pytest.raises(type(error)):
        main.create_context(tmp_path)

    assert calls == []
    assert "WENYI_FAILED" not in capsys.readouterr().out


def test_catalog_format_refusal_restores_backup(monkeypatch, tmp_path, capsys):
    calls = []

    def refusing_backend(*args, **kwargs):
        raise RuntimeError(
            "This is not a current-format Desktop catalog. "
            "Choose a new Desktop data directory; legacy data is not migrated."
        )

    monkeypatch.setattr(main, "LocalBackend", refusing_backend)
    monkeypatch.setattr(main, "restore_latest_backup", _restore_recorder(calls))
    monkeypatch.chdir(tmp_path)

    with pytest.raises(RuntimeError):
        main.create_context(tmp_path)

    assert calls == [tmp_path]
    payload = json.loads(capsys.readouterr().out.split("WENYI_FAILED ", 1)[1])
    assert payload["reason"] == "migration"
    assert payload["restored"] is True
    assert payload["legacy_data"] is True
    assert payload["guidance"]


def test_schema_too_new_restores_backup(monkeypatch, tmp_path, capsys):
    calls = []

    def too_new_backend(*args, **kwargs):
        raise SchemaTooNew("catalog schema 3 is newer than supported schema 2")

    monkeypatch.setattr(main, "LocalBackend", too_new_backend)
    monkeypatch.setattr(main, "restore_latest_backup", _restore_recorder(calls))
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SchemaTooNew):
        main.create_context(tmp_path)

    assert calls == [tmp_path]
    payload = json.loads(capsys.readouterr().out.split("WENYI_FAILED ", 1)[1])
    assert payload["reason"] == "migration"
    assert payload["restored"] is True
    assert "legacy_data" not in payload
