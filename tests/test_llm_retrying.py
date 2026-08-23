"""远端 LLM 的统一选择性重试与事件记录测试。"""

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

from trans_novel.config import Config, LLMConfig, TierConfig
from trans_novel.llm.providers.deepseek import DeepSeekClient
from trans_novel.llm.retrying import (
    EmptyResponseError,
    is_retryable_provider_error,
    retry_reason,
)
from trans_novel.pipeline.orchestrator import Orchestrator
from trans_novel.pipeline.runstore import RunStore


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
    return LLMConfig(
        provider="deepseek",
        base_url="https://example.invalid/v1",
        api_key_env="TEST_LLM_KEY",
        timeout=1,
        max_retries=max_retries,
        tiers={"strong": TierConfig(model="test-model")},
    )


@pytest.mark.parametrize("status", [408, 409, 429, 500, 502, 599])
def test_transient_http_statuses_are_retryable(status: int):
    assert is_retryable_provider_error(_HttpError(status))
    assert retry_reason(_HttpError(status)) == f"http_{status}"


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 422])
def test_permanent_http_statuses_are_not_retryable(status: int):
    assert not is_retryable_provider_error(_HttpError(status))


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


def test_openai_sdk_retry_is_disabled():
    client = DeepSeekClient(_config(max_retries=4))
    with (
        patch.dict(os.environ, {"TEST_LLM_KEY": "secret"}),
        patch("openai.OpenAI") as openai_type,
    ):
        client._ensure_client()

    openai_type.assert_called_once_with(
        api_key="secret",
        base_url="https://example.invalid/v1",
        timeout=1,
        max_retries=0,
    )


def test_transient_error_retries_once_and_records_wait_event():
    client = DeepSeekClient(_config(max_retries=1))
    stub = _ClientStub(
        [
            _HttpError(502, headers={"retry-after-ms": "0"}),
            _response(),
        ]
    )
    client._client = stub
    events: list[dict[str, Any]] = []
    client.set_event_sink(lambda event, **data: events.append({"event": event, **data}))

    assert client.complete([{"role": "user", "content": "x"}], stage="Translator") == "ok"
    assert stub.completions.calls == 2
    assert [event["event"] for event in events] == ["llm_retry_wait"]
    assert events[0]["reason"] == "http_502"
    assert events[0]["failed_attempt"] == 1
    assert events[0]["next_attempt"] == 2
    assert events[0]["wait_seconds"] == 0
    assert events[0]["wait_source"] == "server"
    assert events[0]["stage"] == "Translator"
    assert events[0]["request_id"] == "req-test"


def test_retry_exhaustion_is_recorded_and_reraises_last_error():
    client = DeepSeekClient(_config(max_retries=2))
    failures = [
        _HttpError(503, headers={"retry-after-ms": "0"}),
        _HttpError(503, headers={"retry-after-ms": "0"}),
        _HttpError(503, headers={"retry-after-ms": "0"}),
    ]
    stub = _ClientStub(failures)
    client._client = stub
    events: list[dict[str, Any]] = []
    client.set_event_sink(lambda event, **data: events.append({"event": event, **data}))

    with pytest.raises(_HttpError):
        client.complete([{"role": "user", "content": "x"}], stage="Analyzer")

    assert stub.completions.calls == 3
    assert [event["event"] for event in events] == [
        "llm_retry_wait",
        "llm_retry_wait",
        "llm_retry_exhausted",
    ]
    assert events[-1]["attempts"] == 3
    assert events[-1]["stage"] == "Analyzer"


def test_permanent_error_is_not_retried_or_reported_as_exhaustion():
    client = DeepSeekClient(_config(max_retries=4))
    stub = _ClientStub([_HttpError(401)])
    client._client = stub
    events: list[dict[str, Any]] = []
    client.set_event_sink(lambda event, **data: events.append({"event": event, **data}))

    with pytest.raises(_HttpError):
        client.complete([{"role": "user", "content": "x"}])

    assert stub.completions.calls == 1
    assert events == []


def test_orchestrator_retry_sink_writes_book_event_log():
    with tempfile.TemporaryDirectory() as directory:
        store = RunStore(directory)
        client = DeepSeekClient(_config(max_retries=0))
        orchestrator = Orchestrator(Config(), client=client)
        orchestrator._runtime.bind_llm_events(store)

        client._emit_event("llm_retry_wait", reason="http_502", wait_seconds=1.0)

        with open(store.event_log_path, encoding="utf-8") as file:
            event = json.loads(file.readline())
        assert event["event"] == "llm_retry_wait"
        assert event["reason"] == "http_502"
