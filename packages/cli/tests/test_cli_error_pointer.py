"""Unexpected CLI failures print a concise localization pointer, not a traceback."""

import json
import os
import time

from typer.testing import CliRunner
from wenyi_cli.cli import create_app
from wenyi_cli.commands.error_pointer import last_event_entry, latest_event_log_path


def _write_events(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_unexpected_failure_prints_pointer_without_traceback(tmp_path):
    (tmp_path / "config.yaml").write_text("", encoding="utf-8")
    app = create_app()

    @app.command()
    def probe():
        raise RuntimeError("boom detail")

    result = CliRunner().invoke(app, ["-c", str(tmp_path / "config.yaml"), "probe"])
    assert result.exit_code == 1
    assert "Unexpected error: RuntimeError: boom detail" in result.output
    assert "Run ID: " in result.output
    assert "Full traceback: see the diagnostic log above." in result.output
    assert "Traceback" not in result.output
    [log_path] = (tmp_path / "logs").glob("wenyi-cli-*.log")
    assert "Traceback" in log_path.read_text(encoding="utf-8")


def test_unexpected_failure_points_to_last_event_of_active_run(tmp_path):
    (tmp_path / "config.yaml").write_text("", encoding="utf-8")
    events = tmp_path / "state" / "book" / "targets" / "zh" / "events.jsonl"
    _write_events(
        events,
        [{"event": "translation_prepared"}, {"event": "chapter_started", "chapter": 3}],
    )
    app = create_app()

    @app.command()
    def probe():
        raise KeyError("chapter")

    result = CliRunner().invoke(app, ["-c", str(tmp_path / "config.yaml"), "probe"])
    assert result.exit_code == 1
    assert "Last event: #2 chapter_started" in result.output
    assert str(events) in result.output


def test_pointer_without_run_state_still_names_run_id(tmp_path):
    (tmp_path / "config.yaml").write_text("", encoding="utf-8")
    app = create_app()

    @app.command()
    def probe():
        raise ValueError("no run state here")

    result = CliRunner().invoke(app, ["-c", str(tmp_path / "config.yaml"), "probe"])
    assert result.exit_code == 1
    assert "Unexpected error: ValueError: no run state here" in result.output
    assert "Run ID: " in result.output
    assert "Last event" not in result.output


def test_last_event_entry_skips_torn_final_line(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_bytes(b'{"event": "a"}\n{"event": "b"}\n{"event": "cha')
    assert last_event_entry(path) == (2, "b")
    assert last_event_entry(tmp_path / "missing.jsonl") is None


def test_last_event_entry_ignores_empty_log(tmp_path):
    path = tmp_path / "events.jsonl"
    _write_events(path, [])
    assert last_event_entry(path) is None


def test_latest_event_log_path_prefers_freshest_ledger(tmp_path):
    old = tmp_path / "state" / "old" / "targets" / "zh" / "events.jsonl"
    new = tmp_path / "state" / "new" / "targets" / "ja" / "events.jsonl"
    _write_events(old, [{"event": "old"}])
    _write_events(new, [{"event": "new"}])
    stale = time.time() - 60
    os.utime(old, (stale, stale))
    assert latest_event_log_path(tmp_path) == new
    assert latest_event_log_path(tmp_path / "absent") is None
