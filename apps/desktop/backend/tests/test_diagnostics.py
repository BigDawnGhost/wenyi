"""Desktop diagnostics must not retain content, exceptions or credential values."""

import asyncio
import io
import json
from pathlib import Path

import pytest
from wenyi_desktop import notices
from wenyi_desktop.diagnostics import (
    DiagnosticsMiddleware,
    EngineDiagnostics,
    Reporter,
    emit,
    failure_attributes,
    project_attributes,
    scope,
)


def records(stream):
    return [
        json.loads(line.removeprefix("WENYI_DIAGNOSTIC "))
        for line in stream.getvalue().splitlines()
    ]


def test_shared_native_and_engine_privacy_fixtures():
    fixtures = json.loads((Path(__file__).parents[2] / "diagnostic-fixtures.json").read_text())
    for case in fixtures:
        assert project_attributes(case["input"]) == case["expected"]


def test_only_safe_fields_reach_the_pipe():
    stream = io.StringIO()
    reporter = Reporter(stream)
    secret = "fake-private-book-and-key"
    with scope(reporter, project_ref=secret, run_ref=secret):
        emit("task_failed", error=secret, request_id=secret, category="unknown", count=2)
        emit(secret, count=4)
        emit("llm_retry_wait", provider=secret, model=secret, status_code=429, attempt=2)
    reporter.close()
    assert secret not in stream.getvalue()
    rows = records(stream)
    assert len(rows) == 2
    assert rows[0]["attributes"]["count"] == 2
    assert rows[0]["attributes"]["run_ref"] == reporter.reference(secret)
    assert rows[1]["attributes"]["status_code"] == 429
    assert project_attributes({"count": True, "tokens": -1, "duration_ms": 2**60}) == {}


def test_failure_classifier_does_not_stringify_exception():
    class UnsafeError(TimeoutError):
        def __str__(self):
            raise AssertionError("Do not stringify provider errors")

    assert failure_attributes(UnsafeError()) == {"category": "timeout", "exception": "TimeoutError"}


@pytest.mark.parametrize("code", [0, 3, "fake-private-key"])
def test_system_exit_is_recorded_without_serializing_its_payload(code):
    stream = io.StringIO()
    reporter = Reporter(stream)
    lifecycle = EngineDiagnostics(reporter)
    with pytest.raises(SystemExit):
        with lifecycle.observe("application"):
            raise SystemExit(code)
    lifecycle.stopped()
    reporter.close()
    rows = records(stream)
    assert rows[0]["event"] == "engine_bootstrap_failed"
    assert rows[0]["attributes"]["exception"] == "SystemExit"
    assert rows[-1]["attributes"]["exit_code"] == (code if type(code) is int else 1)
    assert "fake-private" not in stream.getvalue()


def test_first_startup_failure_survives_cleanup_and_uvicorn_system_exit():
    stream = io.StringIO()
    reporter = Reporter(stream)
    lifecycle = EngineDiagnostics(reporter)
    with pytest.raises(SystemExit):
        with lifecycle.observe("bootstrap"):
            try:
                with lifecycle.observe("runtime"):
                    raise PermissionError(13, "fake-private-path")
            except PermissionError:
                with pytest.raises(RuntimeError):
                    with lifecycle.observe("shutdown"):
                        raise RuntimeError("fake-private-cleanup")
                raise SystemExit(3)
    lifecycle.stopped()
    reporter.close()
    rows = records(stream)
    assert len(rows) == 2
    assert rows[0]["attributes"] == {
        "stage": "runtime",
        "category": "permission",
        "exception": "PermissionError",
        "os_error": 13,
    }
    assert rows[-1]["attributes"] == {"stage": "bootstrap", "reason": "failed", "exit_code": 3}
    assert "fake-private" not in stream.getvalue()


def test_running_failure_is_not_misreported_as_bootstrap_failure():
    stream = io.StringIO()
    reporter = Reporter(stream)
    lifecycle = EngineDiagnostics(reporter)
    lifecycle.ready = True
    with pytest.raises(ValueError):
        with lifecycle.observe("running"):
            raise ValueError("fake-private-error")
    lifecycle.stopped()
    reporter.close()
    rows = records(stream)
    assert rows[0]["event"] == "engine_runtime_failed"
    assert rows[-1]["attributes"] == {"stage": "running", "reason": "failed", "exit_code": 1}


def test_ready_frame_is_serialized_and_scopes_do_not_leak():
    stream = io.StringIO()
    reporter = Reporter(stream)
    with scope(reporter, project_ref="one"):
        emit("task_started")
        with scope(reporter, project_ref="two"):
            emit("task_started")
        emit("task_completed")
    emit("task_failed")
    reporter.protocol('WENYI_READY {"protocol":1,"pid":1,"port":1234}\n')
    reporter.close()
    lines = stream.getvalue().splitlines()
    rows = [json.loads(line.removeprefix("WENYI_DIAGNOSTIC ")) for line in lines[:-1]]
    assert len(rows) == 3
    assert rows[0]["attributes"] == rows[2]["attributes"]
    assert rows[0]["attributes"] != rows[1]["attributes"]
    assert lines[-1].startswith("WENYI_READY ")


def test_http_failure_has_correlation_but_no_request_or_exception_content():
    stream = io.StringIO()
    reporter = Reporter(stream)
    sent = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 503, "headers": []})
        await send({"type": "http.response.body", "body": b"fake-private-error"})

    async def send(message):
        sent.append(message)

    asyncio.run(
        DiagnosticsMiddleware(app, reporter)(
            {"type": "http", "method": "POST", "path": "/fake-private-book"}, None, send
        )
    )
    reporter.close()
    row = records(stream)[0]
    headers = dict(sent[0]["headers"])
    assert row["event"] == "http_error"
    assert row["attributes"]["request_id"] == headers[b"x-request-id"].decode()
    assert row["attributes"]["error_id"] == headers[b"x-error-id"].decode()
    assert "fake-private" not in stream.getvalue()


def test_unhandled_http_failure_returns_safe_id_and_cors():
    stream = io.StringIO()
    reporter = Reporter(stream)
    sent = []

    async def app(scope, receive, send):
        raise RuntimeError("fake-secret-in-exception")

    async def send(message):
        sent.append(message)

    asyncio.run(
        DiagnosticsMiddleware(app, reporter, ("tauri://localhost",))(
            {"type": "http", "method": "POST", "headers": [(b"origin", b"tauri://localhost")]},
            None,
            send,
        )
    )
    reporter.close()
    headers = dict(sent[0]["headers"])
    body = json.loads(sent[1]["body"])
    assert body["error_id"] == records(stream)[0]["attributes"]["error_id"]
    assert headers[b"access-control-allow-origin"] == b"tauri://localhost"
    assert "fake-secret" not in str(sent) + stream.getvalue()


def test_legacy_nested_and_malformed_values_are_omitted():
    assert (
        project_attributes(
            {"category": {"secret": "fake-secret"}, "module": ["fake-secret"], "line": "private"}
        )
        == {}
    )


def test_model_observations_keep_client_scope_across_threads_and_leave_ledger_alone():
    import threading

    from wenyi_core.llm.configuration import LLMConfig
    from wenyi_desktop.diagnostic_client import DesktopLLMClient

    stream = io.StringIO()
    reporter = Reporter(stream)
    config = LLMConfig.model_validate(
        {
            "providers": {"default": {"kind": "fake"}},
            "models": {"one": {"provider": "default", "model": "offline"}},
            "tiers": {"strong": "one", "cheap": "one", "fast": "one"},
        }
    )
    with scope(reporter, project_ref="first"):
        client = DesktopLLMClient(config, credentials={})
    original_events = []
    client.set_event_sink(lambda event, **data: original_events.append((event, data)))
    before = client.usage_summary()
    with scope(reporter, project_ref="other"):

        def run():
            client._emit_event("llm_request_started", call_id="call", operation="translation.body")
            client._emit_event("llm_usage", call_id="call", total_tokens=12)
            client._emit_event(
                "llm_request_completed", call_id="call", messages="fake-private-text"
            )

        thread = threading.Thread(target=run)
        thread.start()
        thread.join(timeout=5)
    reporter.close()
    rows = records(stream)
    assert len(rows) == len(original_events) == 3
    assert rows[-1]["attributes"]["tokens"] == 12
    assert rows[0]["attributes"]["phase"] == "translation"
    assert all(row["attributes"]["project_ref"] == reporter.reference("first") for row in rows)
    assert "fake-private-text" not in stream.getvalue()
    assert client.usage_summary() == before


def test_status_notices_are_separate_bounded_and_free_of_error_text():
    stream = io.StringIO()
    reporter = Reporter(stream)
    assert reporter.notice(
        notices.task_notice(
            "task_failed",
            project={"id": "abc123", "name": "A\nBook"},
            run_id="run1",
            task="translation",
            reason="io",
        )
    )
    # Missing reason, unknown reason, and unknown fields are dropped.
    for invalid in (
        {
            "kind": "task_failed",
            "project_id": "abc",
            "project_name": "Book",
            "run_id": "r",
            "task": "translation",
        },
        notices.task_notice(
            "task_failed",
            project={"id": "abc", "name": "Book"},
            run_id="r",
            task="translation",
            reason="fake-private-reason",
        ),
        {
            "kind": "task_failed",
            "project_id": "abc",
            "project_name": "Book",
            "run_id": "r",
            "task": "translation",
            "reason": "io",
            "error": "fake-private-error",
        },
    ):
        assert not reporter.notice(invalid)
    reporter.close()
    lines = [line for line in stream.getvalue().splitlines() if line.startswith("WENYI_NOTICE ")]
    assert len(lines) == 1
    payload = json.loads(lines[0].removeprefix("WENYI_NOTICE "))
    assert payload == {
        "kind": "task_failed",
        "project_id": "abc123",
        "project_name": "A Book",
        "run_id": "run1",
        "task": "translation",
        "reason": "io",
    }
    assert "fake-private" not in stream.getvalue()


def test_status_notice_wire_format_stays_ascii_and_keeps_the_display_name():
    stream = io.StringIO()
    reporter = Reporter(stream)
    assert reporter.notice(
        notices.task_notice(
            "project_completed",
            project={"id": "abc123", "name": "书名 📚"},
            run_id="run1",
            task="translation",
        )
    )
    reporter.close()
    line = next(line for line in stream.getvalue().splitlines() if line.startswith("WENYI_NOTICE "))
    # A non-UTF-8 Windows pipe must never see raw non-ASCII bytes on this channel.
    assert line.isascii()
    assert json.loads(line.removeprefix("WENYI_NOTICE "))["project_name"] == "书名 📚"
