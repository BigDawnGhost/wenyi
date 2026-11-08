"""Logging assembly must stay additive and never silence existing handlers."""

import io
import json
import logging
import os
import stat
import subprocess
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest
from wenyi_core import observability


@pytest.fixture(autouse=True)
def restore_logging():
    root = logging.getLogger()
    before = list(root.handlers)
    level = root.level
    yield
    for handler in list(root.handlers):
        if handler not in before:
            root.removeHandler(handler)
            handler.close()
    for handler in before:
        if handler not in root.handlers:
            root.addHandler(handler)
    root.setLevel(level)


def test_human_format_writes_a_readable_line():
    stream = io.StringIO()
    observability.configure_logging(level="INFO", json=False, service="tests", stream=stream)
    logging.getLogger("wenyi.test").info("hello")
    output = stream.getvalue()
    assert "INFO" in output
    assert "wenyi.test: hello" in output


def test_json_format_emits_one_parseable_object():
    stream = io.StringIO()
    observability.configure_logging(level="INFO", json=True, service="tests", stream=stream)
    logging.getLogger("wenyi.test").info("hello %s", "world")
    lines = [line for line in stream.getvalue().splitlines() if line.strip()]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["level"] == "INFO"
    assert payload["logger"] == "wenyi.test"
    assert payload["message"] == "hello world"
    assert payload["service"] == "tests"
    assert payload["request_id"] == "-"


def test_json_format_includes_the_bound_request_id():
    stream = io.StringIO()
    observability.configure_logging(level="INFO", json=True, service="tests", stream=stream)
    token = observability.bind_request_id("abc123")
    try:
        logging.getLogger("wenyi.test").warning("during request")
    finally:
        observability.reset_request_id(token)
    payload = json.loads(stream.getvalue().strip())
    assert payload["request_id"] == "abc123"
    assert observability.current_request_id() is None


def test_file_logging_rotates_privately_and_releases_the_replaced_sink(tmp_path, capsys):
    path = tmp_path / "diagnostic.log"
    observability.configure_logging(service="tests", json=False, file_path=path)
    [handler] = [h for h in logging.getLogger().handlers if getattr(h, "_wenyi_handler", False)]
    assert isinstance(handler, RotatingFileHandler)
    assert handler.maxBytes == 5 * 1024 * 1024
    assert handler.backupCount == 2
    handler.maxBytes = 200
    for index in range(12):
        logging.getLogger("wenyi.test").warning("diagnostic %s: 你好 %s", index, "x" * 80)
    files = sorted(tmp_path.glob("diagnostic.log*"))
    assert [file.name for file in files] == [
        "diagnostic.log",
        "diagnostic.log.1",
        "diagnostic.log.2",
    ]
    assert all("你好" in file.read_text(encoding="utf-8") for file in files)
    if os.name == "posix":
        assert all(stat.S_IMODE(file.stat().st_mode) == 0o600 for file in files)
    assert capsys.readouterr().err == ""
    observability.configure_logging(stream=io.StringIO())
    assert handler.stream is None


def test_unopenable_file_does_not_displace_existing_logging(tmp_path):
    stream = io.StringIO()
    observability.configure_logging(stream=stream)
    before = list(logging.getLogger().handlers)
    with pytest.raises(OSError):
        observability.configure_logging(file_path=tmp_path / "missing" / "diagnostic.log")
    assert logging.getLogger().handlers == before
    logging.getLogger("wenyi.test").warning("still configured")
    assert "still configured" in stream.getvalue()


def test_reconfiguring_keeps_handlers_installed_by_others():
    root = logging.getLogger()
    foreign = logging.StreamHandler(io.StringIO())
    root.addHandler(foreign)
    try:
        observability.configure_logging(level="INFO", json=False, service="tests")
        assert foreign in root.handlers
        installed = [h for h in root.handlers if getattr(h, "_wenyi_handler", False)]
        assert len(installed) == 1
        observability.configure_logging(level="INFO", json=False, service="tests")
        installed = [h for h in root.handlers if getattr(h, "_wenyi_handler", False)]
        assert len(installed) == 1
        assert foreign in root.handlers
    finally:
        root.removeHandler(foreign)


def test_json_format_is_selected_by_environment(monkeypatch):
    monkeypatch.setenv(observability.JSON_LOG_FORMAT_ENV, "json")
    assert observability.use_json_logs() is True
    monkeypatch.setenv(observability.JSON_LOG_FORMAT_ENV, "text")
    assert observability.use_json_logs() is False
    assert observability.use_json_logs(True) is True


def test_docker_and_application_install_one_correlated_json_handler(monkeypatch):
    monkeypatch.delenv(observability.JSON_LOG_FORMAT_ENV, raising=False)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import json
import logging
import logging.config
from wenyi_core.observability import bind_request_id, configure_logging
with open("docker/logging.json", encoding="utf-8") as handle:
    logging.config.dictConfig(json.load(handle))
configure_logging(service="wenyi-api")
configure_logging(service="wenyi-api")
bind_request_id("test-request")
logging.getLogger("wenyi.test").warning("one record")
""",
        ],
        cwd=Path(__file__).resolve().parents[3],
        capture_output=True,
        text=True,
        check=True,
    )
    records = result.stderr.splitlines()
    assert len(records) == 1, records
    assert json.loads(records[0])["request_id"] == "test-request"
    assert json.loads(records[0])["service"] == "wenyi-api"
