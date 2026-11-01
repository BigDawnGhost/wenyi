"""Logging assembly must stay additive and never silence existing handlers."""

import io
import json
import logging

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
