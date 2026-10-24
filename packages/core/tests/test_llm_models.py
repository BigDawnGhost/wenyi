"""Offline contracts for listing a connection's models through injected SDK clients."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError, APIStatusError
from wenyi_core.llm.configuration import ProviderConfig
from wenyi_core.llm.models import MAX_LISTED_MODELS, list_connection_models
from wenyi_core.llm.providers._openai_compatible import OpenAICompatibleBaseClient
from wenyi_core.llm.providers.gemini import GeminiClient

SECRET = "fake-listing-secret-not-a-real-key"


def _status_error(status: int) -> APIStatusError:
    request = httpx.Request(
        "GET", "https://example.invalid/models", headers={"Authorization": f"Bearer {SECRET}"}
    )
    return APIStatusError(
        f"Bearer {SECRET}", response=httpx.Response(status, request=request), body={"k": SECRET}
    )


@pytest.fixture
def sdk(monkeypatch):
    """Replace the OpenAI SDK client; ``state.listing`` is a list of ids or an exception."""
    state = SimpleNamespace(listing=["b-model", "a-model"], clients=[], calls=0)

    def listing():
        state.calls += 1
        if isinstance(state.listing, Exception):
            raise state.listing
        return [SimpleNamespace(id=name) for name in state.listing]

    def ensure(self):
        state.clients.append(
            {"key": self.api_key(), "timeout": self.cfg.timeout, "retries": self.cfg.max_retries}
        )
        self.validate_credentials()
        return SimpleNamespace(models=SimpleNamespace(list=listing))

    monkeypatch.setattr(OpenAICompatibleBaseClient, "_ensure_client", ensure)
    return state


def _connection(**extra) -> ProviderConfig:
    return ProviderConfig(kind="openai", api_key_env="LIST_KEY", **extra)


def test_lists_sorted_unique_models_with_one_unretried_request(sdk, monkeypatch):
    monkeypatch.setenv("LIST_KEY", SECRET)
    sdk.listing = ["b-model", "a-model", "b-model", " ", "c-model"]
    result = list_connection_models(_connection(timeout=600, max_retries=4), timeout=5)
    assert result.ok
    assert result.models == ("a-model", "b-model", "c-model")
    assert sdk.calls == 1
    assert sdk.clients == [{"key": SECRET, "timeout": 5, "retries": 0}]


def test_injected_credential_overrides_environment(sdk, monkeypatch):
    monkeypatch.setenv("LIST_KEY", "environment-value")
    result = list_connection_models(_connection(), credential="typed-key", use_credential=True)
    assert result.ok
    assert sdk.clients[0]["key"] == "typed-key"


def test_missing_credential_skips_the_request(sdk, monkeypatch):
    monkeypatch.delenv("LIST_KEY", raising=False)
    result = list_connection_models(_connection())
    assert (result.ok, result.category) == (False, "missing_credential")
    assert "LIST_KEY" in result.message
    assert sdk.calls == 0
    injected = list_connection_models(_connection(), credential=None, use_credential=True)
    assert injected.category == "missing_credential"


@pytest.mark.parametrize(
    ("status", "category"),
    [
        (401, "authentication_failed"),
        (404, "model_or_endpoint_not_found"),
        (503, "provider_unavailable"),
    ],
)
def test_http_failures_are_classified_without_leaking_secrets(sdk, monkeypatch, status, category):
    monkeypatch.setenv("LIST_KEY", SECRET)
    sdk.listing = _status_error(status)
    result = list_connection_models(_connection())
    assert (result.ok, result.category, result.status_code) == (False, category, status)
    assert SECRET not in str(result.describe())
    assert sdk.calls == 1


def test_connection_errors_are_reported(sdk, monkeypatch):
    monkeypatch.setenv("LIST_KEY", SECRET)
    sdk.listing = APIConnectionError(request=httpx.Request("GET", "https://example.invalid"))
    assert list_connection_models(_connection()).category == "connection_failed"


def test_listing_is_bounded(sdk, monkeypatch):
    monkeypatch.setenv("LIST_KEY", SECRET)
    sdk.listing = [f"m{index:05d}" for index in range(MAX_LISTED_MODELS + 10)]
    assert len(list_connection_models(_connection()).models) == MAX_LISTED_MODELS


def test_unsupported_adapters_report_a_category():
    from wenyi_core.llm.providers.fake import FakeProvider
    from wenyi_core.llm.transport import ProviderAdapter

    assert list_connection_models(ProviderConfig(kind="fake")).models == ("fake",)
    original = FakeProvider.list_models
    try:
        FakeProvider.list_models = ProviderAdapter.list_models
        result = list_connection_models(ProviderConfig(kind="fake"))
    finally:
        FakeProvider.list_models = original
    assert (result.ok, result.category) == (False, "unsupported")


def test_gemini_keeps_generation_models_without_prefix(monkeypatch):
    items = [
        SimpleNamespace(name="models/gemini-a", supported_actions=["generateContent"]),
        SimpleNamespace(name="models/embedding", supported_actions=["embedContent"]),
        SimpleNamespace(name="models/gemini-b", supported_actions=None),
    ]
    monkeypatch.setattr(
        GeminiClient,
        "_ensure_client",
        lambda self: SimpleNamespace(models=SimpleNamespace(list=lambda: items)),
    )
    result = list_connection_models(
        ProviderConfig(kind="gemini"), credential="fake-gemini", use_credential=True
    )
    assert result.models == ("gemini-a", "gemini-b")


def test_invalid_timeout_and_control_characters_are_rejected():
    with pytest.raises(ValueError, match="Timeout"):
        list_connection_models(_connection(), timeout=0)
    with pytest.raises(ValueError, match="control characters"):
        list_connection_models(_connection(), credential="bad\nkey", use_credential=True)
