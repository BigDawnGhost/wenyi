"""Rotation settings are configurable and every CLI log line carries the run ID."""

import json
import logging
import re
from logging.handlers import RotatingFileHandler

from typer.testing import CliRunner
from wenyi_cli.cli import create_app
from wenyi_core.observability import (
    DEFAULT_LOG_BACKUP_COUNT,
    DEFAULT_LOG_MAX_BYTES,
    LOG_BACKUP_COUNT_ENV,
    LOG_MAX_BYTES_ENV,
    bind_run_id,
    configure_logging,
    current_run_id,
    reset_run_id,
)

RUN_ID_PATTERN = re.compile(r"\[-\] \[([0-9a-f]{32})\] ")


def _owned_file_handlers() -> list[logging.Handler]:
    return [
        handler
        for handler in logging.getLogger().handlers
        if getattr(handler, "_wenyi_handler", False) and isinstance(handler, RotatingFileHandler)
    ]


def _clear_owned_handlers() -> None:
    root = logging.getLogger()
    for handler in [item for item in root.handlers if getattr(item, "_wenyi_handler", False)]:
        root.removeHandler(handler)
        handler.close()


def _probe_app(installed: list[logging.Handler]):
    app = create_app()

    @app.command()
    def probe():
        installed.extend(_owned_file_handlers())
        logging.getLogger("wenyi.probe").info("hello")

    return app


def _invoke_probe(tmp_path, app) -> None:
    result = CliRunner().invoke(app, ["-c", str(tmp_path / "config.yaml"), "probe"])
    assert result.exit_code == 0, result.output


def test_invocation_logging_keeps_default_rotation(tmp_path):
    (tmp_path / "config.yaml").write_text("", encoding="utf-8")
    installed: list[logging.Handler] = []
    _invoke_probe(tmp_path, _probe_app(installed))
    [handler] = installed
    assert handler.maxBytes == DEFAULT_LOG_MAX_BYTES
    assert handler.backupCount == DEFAULT_LOG_BACKUP_COUNT


def test_rotation_settings_follow_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(LOG_MAX_BYTES_ENV, "1024")
    monkeypatch.setenv(LOG_BACKUP_COUNT_ENV, "3")
    (tmp_path / "config.yaml").write_text("", encoding="utf-8")
    installed: list[logging.Handler] = []
    _invoke_probe(tmp_path, _probe_app(installed))
    [handler] = installed
    assert handler.maxBytes == 1024
    assert handler.backupCount == 3


def test_invalid_rotation_environment_falls_back_to_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv(LOG_MAX_BYTES_ENV, "not-a-number")
    monkeypatch.setenv(LOG_BACKUP_COUNT_ENV, "")
    (tmp_path / "config.yaml").write_text("", encoding="utf-8")
    installed: list[logging.Handler] = []
    _invoke_probe(tmp_path, _probe_app(installed))
    [handler] = installed
    assert handler.maxBytes == DEFAULT_LOG_MAX_BYTES
    assert handler.backupCount == DEFAULT_LOG_BACKUP_COUNT


def test_explicit_rotation_arguments_win_over_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(LOG_MAX_BYTES_ENV, "1024")
    try:
        configure_logging(
            json=False, file_path=tmp_path / "diag.log", max_bytes=2048, backup_count=7
        )
        [handler] = _owned_file_handlers()
        assert handler.maxBytes == 2048
        assert handler.backupCount == 7
    finally:
        _clear_owned_handlers()


def test_configured_size_rolls_log_files(tmp_path, monkeypatch):
    monkeypatch.setenv(LOG_MAX_BYTES_ENV, "300")
    try:
        configure_logging(json=False, file_path=tmp_path / "diag.log")
        for index in range(6):
            logging.getLogger("wenyi.test").warning("diagnostic %s %s", index, "x" * 80)
    finally:
        _clear_owned_handlers()
    files = sorted(tmp_path.glob("diag.log*"))
    assert len(files) >= 2
    assert all("diagnostic" in file.read_text(encoding="utf-8") for file in files)


def test_invocation_log_lines_carry_a_stable_run_id(tmp_path):
    (tmp_path / "config.yaml").write_text("", encoding="utf-8")
    app = _probe_app([])
    run_ids = []
    for _ in range(2):
        _invoke_probe(tmp_path, app)
    for path in sorted((tmp_path / "logs").glob("wenyi-cli-*.log")):
        lines = path.read_text(encoding="utf-8").splitlines()
        assert lines
        found = set()
        for line in lines:
            match = RUN_ID_PATTERN.search(line)
            assert match, line
            found.add(match.group(1))
        assert len(found) == 1
        run_ids.append(found.pop())
    assert len(run_ids) == 2
    assert run_ids[0] != run_ids[1]


def test_json_invocation_logs_carry_run_id(tmp_path, monkeypatch):
    monkeypatch.setenv("WENYI_LOG_FORMAT", "json")
    (tmp_path / "config.yaml").write_text("", encoding="utf-8")
    _invoke_probe(tmp_path, _probe_app([]))
    [path] = (tmp_path / "logs").glob("wenyi-cli-*.log")
    [record] = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert re.fullmatch(r"[0-9a-f]{32}", record["run_id"])
    assert record["logger"] == "wenyi.probe"


def test_run_id_binding_round_trip():
    assert current_run_id() is None
    token = bind_run_id("run-123")
    try:
        assert current_run_id() == "run-123"
    finally:
        reset_run_id(token)
    assert current_run_id() is None
