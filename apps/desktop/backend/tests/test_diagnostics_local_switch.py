"""Without a Tauri receiver, the local diagnostic switch records instead of swallowing."""

import io
import json
import logging

import pytest
from wenyi_desktop.diagnostic_client import DesktopLLMClient
from wenyi_desktop.diagnostics import (
    LOCAL_DIAGNOSTICS_ENV,
    EngineDiagnostics,
    Reporter,
    emit,
    local_diagnostics_enabled,
    scope,
)


def local_records(caplog):
    return [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == "wenyi_desktop.diagnostics"
    ]


def test_switch_defaults_to_off_and_parses_explicit_values(monkeypatch):
    monkeypatch.delenv(LOCAL_DIAGNOSTICS_ENV, raising=False)
    assert local_diagnostics_enabled() is False
    for value in ("1", "true", "YES", "on"):
        monkeypatch.setenv(LOCAL_DIAGNOSTICS_ENV, value)
        assert local_diagnostics_enabled() is True
    for value in ("", "0", "false", "off", "junk"):
        monkeypatch.setenv(LOCAL_DIAGNOSTICS_ENV, value)
        assert local_diagnostics_enabled() is False


def test_module_emit_without_receiver_stays_silent_by_default(monkeypatch, caplog):
    monkeypatch.delenv(LOCAL_DIAGNOSTICS_ENV, raising=False)
    caplog.set_level(logging.INFO, logger="wenyi_desktop.diagnostics")
    with scope(None):
        emit("task_started", kind="translation")
    assert local_records(caplog) == []


def test_module_emit_without_receiver_records_locally_when_switched_on(monkeypatch, caplog):
    monkeypatch.setenv(LOCAL_DIAGNOSTICS_ENV, "1")
    caplog.set_level(logging.INFO, logger="wenyi_desktop.diagnostics")
    with scope(None):
        emit("task_failed", kind="translation", error="fake-private-error", count=2)
    rows = local_records(caplog)
    assert rows == [{"event": "task_failed", "attributes": {"kind": "translation", "count": 2}}]
    assert "fake-private" not in json.dumps(rows)


def test_switch_on_with_receiver_still_uses_the_pipe_only(monkeypatch, caplog):
    monkeypatch.setenv(LOCAL_DIAGNOSTICS_ENV, "1")
    caplog.set_level(logging.INFO, logger="wenyi_desktop.diagnostics")
    stream = io.StringIO()
    reporter = Reporter(stream)
    with scope(reporter):
        emit("task_started", kind="translation")
    reporter.close()
    assert local_records(caplog) == []
    assert "task_started" in stream.getvalue()


def test_engine_diagnostics_records_locally_without_reporter(monkeypatch, caplog):
    monkeypatch.setenv(LOCAL_DIAGNOSTICS_ENV, "1")
    caplog.set_level(logging.INFO, logger="wenyi_desktop.diagnostics")
    lifecycle = EngineDiagnostics(None)
    with pytest.raises(ValueError):
        with lifecycle.observe("bootstrap"):
            raise ValueError("fake-private-failure")
    lifecycle.stopped()
    rows = local_records(caplog)
    assert [row["event"] for row in rows] == ["engine_bootstrap_failed", "engine_stopped"]
    assert rows[0]["attributes"]["exception"] == "ValueError"
    assert rows[0]["attributes"]["category"] == "invalid"
    assert rows[1]["attributes"]["reason"] == "failed"
    assert all("fake-private" not in json.dumps(row) for row in rows)


def test_llm_client_records_locally_without_reporter(monkeypatch, caplog):
    from wenyi_core.llm.configuration import LLMConfig
    from wenyi_core.llm.operation_names import OPERATION_NAMES

    monkeypatch.setenv(LOCAL_DIAGNOSTICS_ENV, "1")
    caplog.set_level(logging.INFO, logger="wenyi_desktop.diagnostics")
    config = LLMConfig.model_validate(
        {
            "providers": {"default": {"kind": "fake"}},
            "models": {"default/offline": {}},
            "routes": {op: {"model": "default/offline"} for op in OPERATION_NAMES},
        }
    )
    with scope(None):
        client = DesktopLLMClient(config, credentials={})
    client._emit_event(
        "llm_retry_wait", call_id="call", status_code=429, attempt=2, messages="fake-private-text"
    )
    rows = local_records(caplog)
    assert rows == [{"event": "llm_retry_wait", "attributes": {"status_code": 429, "attempt": 2}}]


class _BrokenStream(io.StringIO):
    def write(self, value):
        raise OSError("fake receiver gone")

    def flush(self):
        raise OSError("fake receiver gone")


def test_reporter_lost_receiver_records_warning_when_switched_on(monkeypatch, caplog):
    monkeypatch.setenv(LOCAL_DIAGNOSTICS_ENV, "1")
    caplog.set_level(logging.WARNING, logger="wenyi_desktop.diagnostics")
    reporter = Reporter(_BrokenStream())
    with scope(reporter):
        emit("task_started")
    reporter.close()
    assert reporter.closed is True
    assert any("receiver lost" in record.getMessage() for record in caplog.records)


def test_reporter_lost_receiver_stays_silent_by_default(monkeypatch, caplog):
    monkeypatch.delenv(LOCAL_DIAGNOSTICS_ENV, raising=False)
    caplog.set_level(logging.WARNING, logger="wenyi_desktop.diagnostics")
    reporter = Reporter(_BrokenStream())
    with scope(reporter):
        emit("task_started")
    reporter.close()
    assert reporter.closed is True
    assert not any("receiver lost" in record.getMessage() for record in caplog.records)
