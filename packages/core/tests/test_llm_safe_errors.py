"""Credential-scoped errors must be safe before reaching workflow consumers."""

import json
import logging
import traceback
from types import SimpleNamespace

import httpx
import pytest
from openai import APIStatusError
from wenyi_core.llm.configuration import LLMConfig
from wenyi_core.llm.limits import RequestCancelled, RequestStopped
from wenyi_core.llm.retrying import (
    ProviderRequestError,
    TruncatedResponseError,
    is_resumable_provider_interrupt,
)
from wenyi_core.llm.router import RoutedLLMClient

SECRET = "fake-boundary-secret-not-a-real-key"


def client_with_failure(*, credentials=True, status=503, other_secret=None):
    client = RoutedLLMClient(
        LLMConfig.model_validate(
            {
                "providers": {"default": {"kind": "openai", "max_retries": 1}},
                "models": {"one": {"provider": "default", "model": "offline"}},
                "tiers": {"strong": "one", "cheap": "one", "fast": "one"},
            }
        ),
        credentials={"default": SECRET, "other": other_secret} if credentials else None,
    )
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        response = httpx.Response(
            status,
            headers={"x-request-id": f"request-{SECRET}-{other_secret or ''}", "retry-after": "0"},
            request=httpx.Request(
                "POST", "https://example.invalid", headers={"Authorization": f"Bearer {SECRET}"}
            ),
        )
        raise APIStatusError(
            f"Authorization: Bearer {SECRET}", response=response, body={"key": SECRET}
        ) from RuntimeError(f"Transport context {SECRET}")

    client.adapter("default")._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    return client, calls


@pytest.mark.parametrize(
    "status, attempts, resumable", [(401, 1, False), (402, 1, True), (503, 2, True)]
)
def test_final_provider_failure_and_retry_diagnostics_are_safe(caplog, status, attempts, resumable):
    client, calls = client_with_failure(status=status, other_secret="fake-other-connection-key")
    events = []
    client.set_event_sink(lambda event, **data: events.append((event, data)))
    with pytest.raises(Exception) as caught:
        client.complete([{"role": "user", "content": "Hello"}], operation="review.verify")
    logging.getLogger(__name__).error(
        "Worker failed", exc_info=(type(caught.value), caught.value, caught.tb)
    )
    assert len(calls) == attempts
    assert SECRET not in str(caught.value)
    assert SECRET not in "".join(traceback.format_exception(caught.value))
    assert SECRET not in json.dumps(events)
    assert SECRET not in caplog.text
    assert "fake-other-connection-key" not in json.dumps(events)
    assert "fake-other-connection-key" not in caplog.text
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert isinstance(caught.value, ProviderRequestError)
    assert is_resumable_provider_interrupt(caught.value) is resumable
    assert caught.value.status_code == status
    assert not hasattr(caught.value, "request")
    assert not hasattr(caught.value, "response")


def test_credential_validation_remains_actionable():
    client, _ = client_with_failure()
    missing = RoutedLLMClient(client.config, credentials={"default": None})
    with pytest.raises(RuntimeError, match="Provider openai has no configured credential"):
        missing.validate_credentials()


def test_without_snapshot_keeps_original_exception():
    client, calls = client_with_failure(credentials=False, status=401)
    with pytest.raises(APIStatusError, match=SECRET):
        client.complete([], operation="translation.body")
    assert len(calls) == 1


def test_failed_event_sink_cannot_log_provider_context(caplog):
    client, calls = client_with_failure()

    def sink(event, **data):
        raise RuntimeError(f"Sink failed with {SECRET}")

    client.set_event_sink(sink)
    with pytest.raises(ProviderRequestError):
        client.complete([], operation="translation.body")
    assert len(calls) == 2
    assert "Failed to write LLM event" in caplog.text
    assert SECRET not in caplog.text


def test_scoped_capture_cannot_log_provider_context(caplog):
    client, calls = client_with_failure()
    events = []
    client.set_event_sink(lambda event, **data: events.append((event, data)))

    def observer(event, **data):
        raise RuntimeError(f"Observer failed with {SECRET}")

    with client.capture_events(observer), pytest.raises(ProviderRequestError):
        client.complete([], operation="translation.body")
    assert len(calls) == 2
    assert "Failed to write LLM event" in caplog.text
    assert SECRET not in caplog.text
    assert SECRET not in json.dumps(events)
    failure = next(data for event, data in events if event == "llm_request_failed")
    assert failure["error_category"] == "provider_unavailable"
    assert failure["status_code"] == 503


@pytest.mark.parametrize("stop_type", [RequestStopped, RequestCancelled])
def test_control_flow_errors_are_preserved(stop_type):
    client, calls = client_with_failure()
    stop = stop_type("Pause safely")

    def create(**kwargs):
        raise stop

    client.adapter("default")._client.chat.completions.create = create
    with pytest.raises(stop_type) as caught:
        client.complete([], operation="translation.body")
    assert caught.value is stop


@pytest.mark.parametrize(
    "error, resumable, truncated",
    [
        (RuntimeError(f"insufficient balance {SECRET}"), True, False),
        (TimeoutError(SECRET), True, False),
        (RuntimeError(SECRET), False, False),
        (TruncatedResponseError(SECRET), False, True),
    ],
)
def test_safe_error_preserves_workflow_classification(error, resumable, truncated):
    client, _ = client_with_failure()
    client.limits.wait_for_retry = lambda delay: None

    def create(**kwargs):
        raise error

    client.adapter("default")._client.chat.completions.create = create
    with pytest.raises(ProviderRequestError) as caught:
        client.complete([], operation="translation.body")
    assert is_resumable_provider_interrupt(caught.value) is resumable
    assert isinstance(caught.value, TruncatedResponseError) is truncated
    assert SECRET not in str(caught.value)


def test_fallback_still_sees_original_provider_classification():
    client, calls = client_with_failure()
    data = client.config.model_dump()
    data["providers"]["other"] = {"kind": "openai", "max_retries": 0}
    data["models"]["two"] = {"provider": "other", "model": "fallback"}
    data["routes"]["translation.body"] = {"model": "one", "fallbacks": ["two"]}
    routed = RoutedLLMClient(
        LLMConfig.model_validate(data), credentials={"default": SECRET, "other": "fake-other-key"}
    )
    routed.adapter("default")._client = client.adapter("default")._client
    messages = [{"role": "user", "content": SECRET}]

    def create(**kwargs):
        assert kwargs["messages"] == messages
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=SECRET), finish_reason="stop")]
        )

    routed.adapter("other")._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    assert routed.complete(messages, operation="translation.body") == SECRET
    assert len(calls) == 2


def test_review_persists_only_safe_errors(tmp_path, caplog):
    from wenyi_core.pipeline.orchestrator import Orchestrator

    from tests.review_fixtures import autofix_config as _config
    from tests.review_fixtures import autofix_store as _store

    client, calls = client_with_failure()
    config = _config(str(tmp_path / "state"))
    config.llm = client.config
    config.pipeline.review_autofix = False
    store = _store(str(tmp_path))
    orch = Orchestrator(config, client)
    with pytest.raises(ProviderRequestError):
        orch._review.run_session(store, [])
    assert len(calls) == 2
    artifacts = [path for path in tmp_path.rglob("*.json") if "review" in str(path)]
    assert artifacts
    documents = "\n".join(path.read_text() for path in artifacts)
    assert "Model request failed:" in documents
    assert '"interrupted"' in documents
    assert SECRET not in documents
    for path in tmp_path.rglob("*.jsonl"):
        assert SECRET not in path.read_text()
    assert SECRET not in caplog.text
