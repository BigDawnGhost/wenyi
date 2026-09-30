"""Shared selective retry and event-recording tests for remote LLMs."""

from __future__ import annotations

import json
import os
import tempfile
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from openai import APIConnectionError, APITimeoutError
from wenyi_core.agents.synopsis import Synopsizer
from wenyi_core.agents.translator import Translator
from wenyi_core.config import Config, LLMConfig
from wenyi_core.llm.providers.deepseek import DeepSeekClient
from wenyi_core.llm.retrying import (
    EmptyResponseError,
    is_resumable_provider_interrupt,
    is_retryable_provider_error,
    retry_reason,
)
from wenyi_core.llm.router import RoutedLLMClient
from wenyi_core.pipeline.orchestrator import Orchestrator
from wenyi_core.storage.file import FileStorage
from wenyi_core.storage.protocol import Storage

from tests.model_fixtures import model_config


def require_file_storage(store: Storage) -> FileStorage:
    """CLI/offline tests use the file backend; narrow Storage to FileStorage for path asserts."""
    if not isinstance(store, FileStorage):
        raise TypeError(f"expected FileStorage, got {type(store).__name__}")
    return store


class _HttpError(Exception):
    def __init__(self, status_code: int, *, headers: dict[str, str] | None = None):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code
        self.request_id = "req-test"
        self.response = SimpleNamespace(
            status_code=status_code,
            headers=headers or {},
        )


def _response(content: str = "ok") -> Any:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=None,
    )


class _CompletionsStub:
    def __init__(self, outcomes: list[Any]):
        self.outcomes = list(outcomes)
        self.calls = 0

    def create(self, **kwargs: Any) -> Any:
        del kwargs
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _ClientStub:
    def __init__(self, outcomes: list[Any]):
        self.completions = _CompletionsStub(outcomes)
        self.chat = SimpleNamespace(completions=self.completions)


def _config(*, max_retries: int) -> LLMConfig:
    return model_config(
        kind="deepseek",
        base_url="https://example.invalid/v1",
        api_key_env="TEST_LLM_KEY",
        timeout=1,
        max_retries=max_retries,
        profiles={"strong": dict(model="test-model")},
    )


@pytest.mark.parametrize("status", [408, 409, 429, 500, 502, 599])
def test_transient_http_statuses_are_retryable(status: int):
    assert is_retryable_provider_error(_HttpError(status))
    assert retry_reason(_HttpError(status)) == f"http_{status}"


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 422])
def test_permanent_http_statuses_are_not_retryable(status: int):
    assert not is_retryable_provider_error(_HttpError(status))


@pytest.mark.parametrize("status", [402, 408, 429, 500, 503])
def test_provider_balance_and_transient_stops_are_resumable_interrupts(status: int):
    assert is_resumable_provider_interrupt(_HttpError(status))


def test_insufficient_balance_message_is_resumable_without_status():
    assert is_resumable_provider_interrupt(RuntimeError("Insufficient Balance"))
    assert not is_resumable_provider_interrupt(ValueError("invalid review config"))


def test_server_retry_override_takes_precedence_over_status():
    assert not is_retryable_provider_error(_HttpError(503, headers={"x-should-retry": "false"}))
    assert is_retryable_provider_error(_HttpError(400, headers={"x-should-retry": "true"}))


def test_only_transient_transport_errors_are_retryable():
    request = httpx.Request("POST", "https://example.invalid/v1")
    assert retry_reason(TimeoutError()) == "timeout"
    assert retry_reason(APITimeoutError(request)) == "timeout"
    assert retry_reason(ConnectionError()) == "connection"
    assert retry_reason(APIConnectionError(request=request)) == "connection"
    assert retry_reason(httpx.RemoteProtocolError("remote closed")) == "connection"
    assert retry_reason(httpx.UnsupportedProtocol("bad scheme")) is None
    assert retry_reason(httpx.InvalidURL("bad url")) is None
    assert retry_reason(RuntimeError("application failure")) is None


def test_empty_model_response_is_retryable():
    error = EmptyResponseError("content is empty")

    assert is_retryable_provider_error(error)
    assert retry_reason(error) == "empty_response"


def _truncated_response():
    response = _response("Partial output")
    response.choices[0].finish_reason = "length"
    response.usage = SimpleNamespace(prompt_tokens=2, completion_tokens=3, total_tokens=5)
    return response


@pytest.mark.parametrize("operation", ["synopsis.chapter", "synopsis.book"])
@pytest.mark.parametrize("response_kind", ["truncated", "empty", "whitespace"])
def test_synopsis_incomplete_response_retries_in_shared_transport(
    operation, response_kind, monkeypatch
):
    client = RoutedLLMClient(_config(max_retries=1))
    incomplete = _truncated_response()
    if response_kind != "truncated":
        incomplete.choices[0].finish_reason = "stop"
        incomplete.choices[0].message.content = "" if response_kind == "empty" else " \n\t"
    success = _response("Complete summary.")
    success.usage = SimpleNamespace(prompt_tokens=3, completion_tokens=4, total_tokens=7)
    stub = _ClientStub([incomplete, success])
    client.adapter("default")._client = stub
    monkeypatch.setattr(client.limits, "wait_for_retry", lambda delay: None)
    events = []
    client.set_event_sink(lambda event, **data: events.append({"event": event, **data}))

    assert client.complete([], operation=operation) == "Complete summary."
    assert stub.completions.calls == 2
    waits = [event for event in events if event["event"] == "llm_retry_wait"]
    assert len(waits) == 1
    assert waits[0]["reason"] == (
        "truncated_response" if response_kind == "truncated" else "empty_response"
    )
    assert client.usage_summary()["totals"]["calls"] == 2
    assert client.usage_summary()["totals"]["total_tokens"] == 12


@pytest.mark.parametrize("method", ["digest_chapter", "book_synopsis"])
@pytest.mark.parametrize("max_retries", [0, 1])
def test_summary_exhaustion_respects_provider_attempt_limit(method, max_retries, monkeypatch):
    client = RoutedLLMClient(_config(max_retries=max_retries))
    stub = _ClientStub([_truncated_response() for _ in range(max_retries + 1)])
    client.adapter("default")._client = stub
    monkeypatch.setattr(client.limits, "wait_for_retry", lambda delay: None)
    synopsizer = Synopsizer(client, Config())
    args = ("Source chapter.",) if method == "digest_chapter" else (["Digest."], "")

    assert getattr(synopsizer, method)(*args) == ""
    assert stub.completions.calls == max_retries + 1
    assert client.usage_summary()["totals"]["calls"] == max_retries + 1


@pytest.mark.parametrize("truncated_attempts", [1, 2])
def test_translation_truncation_uses_alignment_retry_and_paragraph_fallback(truncated_attempts):
    client = RoutedLLMClient(_config(max_retries=3))
    successes = (
        [_response('{"translations":["First.","Second."]}')]
        if truncated_attempts == 1
        else [_response('{"translations":["First."]}'), _response('{"translations":["Second."]}')]
    )
    stub = _ClientStub([_truncated_response() for _ in range(truncated_attempts)] + successes)
    client.adapter("default")._client = stub
    cfg = Config()
    cfg.pipeline.align_retry_limit = 1
    translator = Translator(client, cfg)

    assert translator.translate_batch(["First source.", "Second source."]) == ["First.", "Second."]
    assert stub.completions.calls == truncated_attempts + len(successes)
    assert (translator.last_batch_turn is None) == (truncated_attempts == 2)


def test_openai_sdk_retry_is_disabled():
    client = RoutedLLMClient(_config(max_retries=4))
    with (
        patch.dict(os.environ, {"TEST_LLM_KEY": "secret"}),
        patch("openai.OpenAI") as openai_type,
    ):
        adapter = client.adapter("default")
        assert isinstance(adapter, DeepSeekClient)
        adapter._ensure_client()

    openai_type.assert_called_once_with(
        api_key="secret",
        base_url="https://example.invalid/v1",
        timeout=1,
        max_retries=0,
    )


def test_transient_error_retries_once_and_records_wait_event():
    client = RoutedLLMClient(_config(max_retries=1))
    stub = _ClientStub(
        [
            _HttpError(502, headers={"retry-after-ms": "0"}),
            _response(),
        ]
    )
    client.adapter("default")._client = stub
    events: list[dict[str, Any]] = []
    client.set_event_sink(
        lambda event, **data: (
            events.append({"event": event, **data}) if event.startswith("llm_retry_") else None
        )
    )

    assert client.complete([{"role": "user", "content": "x"}], operation="translation.body") == "ok"
    assert stub.completions.calls == 2
    assert [event["event"] for event in events] == ["llm_retry_wait"]
    assert events[0]["reason"] == "http_502"
    assert events[0]["failed_attempt"] == 1
    assert events[0]["next_attempt"] == 2
    assert events[0]["wait_seconds"] == 0
    assert events[0]["wait_source"] == "server"
    assert events[0]["stage"] == "translation.body"
    assert events[0]["request_id"] == "req-test"


def test_retry_exhaustion_is_recorded_and_reraises_last_error():
    client = RoutedLLMClient(_config(max_retries=2))
    failures = [
        _HttpError(503, headers={"retry-after-ms": "0"}),
        _HttpError(503, headers={"retry-after-ms": "0"}),
        _HttpError(503, headers={"retry-after-ms": "0"}),
    ]
    stub = _ClientStub(failures)
    client.adapter("default")._client = stub
    events: list[dict[str, Any]] = []
    client.set_event_sink(
        lambda event, **data: (
            events.append({"event": event, **data}) if event.startswith("llm_retry_") else None
        )
    )

    with pytest.raises(_HttpError):
        client.complete([{"role": "user", "content": "x"}], operation="analysis.style")

    assert stub.completions.calls == 3
    assert [event["event"] for event in events] == [
        "llm_retry_wait",
        "llm_retry_wait",
        "llm_retry_exhausted",
    ]
    assert events[-1]["attempts"] == 3
    assert events[-1]["stage"] == "analysis.style"


def test_permanent_error_is_not_retried_or_reported_as_exhaustion():
    client = RoutedLLMClient(_config(max_retries=4))
    stub = _ClientStub([_HttpError(401)])
    client.adapter("default")._client = stub
    events: list[dict[str, Any]] = []
    client.set_event_sink(
        lambda event, **data: (
            events.append({"event": event, **data}) if event.startswith("llm_retry_") else None
        )
    )

    with pytest.raises(_HttpError):
        client.complete([{"role": "user", "content": "x"}], operation="translation.body")

    assert stub.completions.calls == 1
    assert events == []


def test_orchestrator_retry_sink_writes_book_event_log():
    with tempfile.TemporaryDirectory() as directory:
        store = FileStorage(directory)
        client = RoutedLLMClient(_config(max_retries=0))
        orchestrator = Orchestrator(Config(), client=client)
        orchestrator._runtime.bind_llm_events(store)

        client._emit_event("llm_retry_wait", reason="http_502", wait_seconds=1.0)

        with open(store.event_log_path, encoding="utf-8") as file:
            event = next(
                json.loads(line) for line in file if json.loads(line)["event"] == "llm_retry_wait"
            )
        assert event["event"] == "llm_retry_wait"
        assert event["reason"] == "http_502"
