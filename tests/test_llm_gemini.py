"""Complete unit tests for the Gemini LLM provider."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest
from google.genai.errors import ClientError, ServerError
from pydantic import ValidationError

from tests.model_fixtures import model_config
from trans_novel.config import Config
from trans_novel.llm.factory import build_client
from trans_novel.llm.providers.gemini import (
    GeminiClient,
    GeminiOptions,
    convert_messages_to_gemini,
    extract_gemini_usage,
    get_api_key_from_env,
)
from trans_novel.llm.router import RoutedLLMClient


def test_gemini_tier_options_thinking_mutual_exclusion():
    """Test mutual exclusion of thinking_level and thinking_budget."""
    opt1 = GeminiOptions(thinking_level="high")
    assert opt1.thinking_level == "high"

    opt2 = GeminiOptions(thinking_budget=1024)
    assert opt2.thinking_budget == 1024

    with pytest.raises(
        ValidationError, match="thinking_level and thinking_budget are mutually exclusive"
    ):
        GeminiOptions(thinking_level="high", thinking_budget=1024)


def test_api_key_env_precedence():
    """Test API-key environment-variable precedence and fallbacks."""
    with patch.dict(
        os.environ,
        {
            "CUSTOM_KEY": "custom_val",
            "GEMINI_API_KEY": "gemini_val",
            "GOOGLE_API_KEY": "google_val",
        },
        clear=True,
    ):
        key, env_name = get_api_key_from_env("CUSTOM_KEY")
        assert key == "custom_val"
        assert env_name == "CUSTOM_KEY"

    with patch.dict(
        os.environ,
        {"GEMINI_API_KEY": "gemini_val", "GOOGLE_API_KEY": "google_val"},
        clear=True,
    ):
        key, env_name = get_api_key_from_env()
        assert key == "gemini_val"
        assert env_name == "GEMINI_API_KEY"

    with patch.dict(os.environ, {"GOOGLE_API_KEY": "google_val"}, clear=True):
        key, env_name = get_api_key_from_env()
        assert key == "google_val"
        assert env_name == "GOOGLE_API_KEY"

    with patch.dict(os.environ, {}, clear=True):
        key, env_name = get_api_key_from_env()
        assert key is None
        assert env_name == "GEMINI_API_KEY"


def test_convert_messages_to_gemini():
    """Test conversion from OpenAI-style messages to Gemini format."""
    messages = [
        {"role": "system", "content": "You are a translator."},
        {"role": "system", "content": "Translate carefully."},
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "你好"},
        {"role": "user", "content": "World"},
    ]
    sys_inst, contents = convert_messages_to_gemini(messages)
    assert sys_inst == "You are a translator.\n\nTranslate carefully."
    assert len(contents) == 3
    assert contents[0] == {"role": "user", "parts": [{"text": "Hello"}]}
    assert contents[1] == {"role": "model", "parts": [{"text": "你好"}]}
    assert contents[2] == {"role": "user", "parts": [{"text": "World"}]}


def test_extract_gemini_usage():
    """Test extraction and accounting of Gemini tokens and cache tokens."""
    usage_meta = SimpleNamespace(
        prompt_token_count=100,
        candidates_token_count=50,
        thoughts_token_count=25,
        total_token_count=175,
        cached_content_token_count=30,
    )
    sample = extract_gemini_usage(usage_meta)
    assert sample is not None
    assert sample.prompt_tokens == 100
    assert sample.completion_tokens == 75
    assert sample.total_tokens == 175
    assert sample.cache_hit_tokens == 30
    assert sample.cache_miss_tokens == 70


def test_gemini_client_validate_credentials():
    """Test client credential validation."""
    cfg = model_config(kind="gemini", api_key_env="TEST_MISSING_ENV_KEY")
    client = RoutedLLMClient(cfg)

    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(RuntimeError, match="Environment variable"):
            client.validate_credentials()

    with patch.dict(os.environ, {"TEST_MISSING_ENV_KEY": "valid_key"}):
        client.validate_credentials()


def test_gemini_client_applies_timeout_in_milliseconds():
    """Convert the shared timeout in seconds to google-genai milliseconds."""
    cfg = model_config(
        kind="gemini",
        api_key_env="TEST_GEMINI_KEY",
        timeout=17,
    )

    with (
        patch.dict(os.environ, {"TEST_GEMINI_KEY": "valid_key"}, clear=True),
        patch("google.genai.Client") as client_type,
    ):
        adapter = RoutedLLMClient(cfg).adapter("default")
        assert isinstance(adapter, GeminiClient)
        adapter._ensure_client()

    client_type.assert_called_once_with(
        api_key="valid_key",
        http_options={"timeout": 17_000, "retry_options": {"attempts": 1}},
    )


def test_gemini_client_complete_and_usage():
    """Test GeminiClient.complete and usage attribution."""
    cfg = model_config(
        kind="gemini",
        api_key_env="TEST_GEMINI_KEY",
        profiles={"strong": dict(model="gemini-3.6-flash", options={"temperature": 0.3})},
    )

    mock_client_instance = MagicMock()
    mock_response = SimpleNamespace(
        text="翻译结果测试",
        candidates=[
            SimpleNamespace(
                finish_reason="STOP",
                content=SimpleNamespace(parts=[SimpleNamespace(text="翻译结果测试")]),
            )
        ],
        usage_metadata=SimpleNamespace(
            prompt_token_count=80,
            candidates_token_count=20,
            thoughts_token_count=5,
            total_token_count=105,
            cached_content_token_count=0,
        ),
    )
    mock_client_instance.models.generate_content.return_value = mock_response

    client = RoutedLLMClient(cfg)
    client.adapter("default")._client = mock_client_instance

    res = client.complete([{"role": "user", "content": "test"}], operation="translation.body")

    assert res == "翻译结果测试"
    mock_client_instance.models.generate_content.assert_called_once()
    call_kwargs = mock_client_instance.models.generate_content.call_args.kwargs
    assert call_kwargs["model"] == "gemini-3.6-flash"
    assert call_kwargs["config"]["temperature"] == 0.3

    summary = client.usage_summary()
    assert summary["totals"]["prompt_tokens"] == 80
    assert summary["totals"]["completion_tokens"] == 25


def test_gemini_client_retries_server_error_and_records_wait():
    """Retry Gemini 5xx errors through the shared policy and emit wait events."""
    cfg = model_config(
        kind="gemini",
        max_retries=1,
        profiles={"strong": dict(model="gemini-3.6-flash")},
    )
    request = httpx.Request("POST", "https://example.invalid")
    failure_response = httpx.Response(
        503,
        request=request,
        headers={"retry-after-ms": "0"},
    )
    failure = ServerError(503, {"message": "unavailable"}, failure_response)
    success = SimpleNamespace(
        text="ok",
        candidates=[SimpleNamespace(finish_reason="STOP")],
        usage_metadata=None,
    )
    sdk = MagicMock()
    sdk.models.generate_content.side_effect = [failure, success]
    client = RoutedLLMClient(cfg)
    client.adapter("default")._client = sdk
    events = []
    client.set_event_sink(
        lambda event, **data: (
            events.append({"event": event, **data}) if event.startswith("llm_retry_") else None
        )
    )

    assert (
        client.complete([{"role": "user", "content": "test"}], operation="translation.body") == "ok"
    )
    assert sdk.models.generate_content.call_count == 2
    assert [item["event"] for item in events] == ["llm_retry_wait"]
    assert events[0]["reason"] == "http_503"


def test_gemini_client_does_not_retry_client_error():
    """Propagate permanent Gemini 4xx errors immediately."""
    cfg = model_config(
        kind="gemini",
        max_retries=4,
        profiles={"strong": dict(model="gemini-3.6-flash")},
    )
    failure = ClientError(401, {"message": "unauthorized"})
    sdk = MagicMock()
    sdk.models.generate_content.side_effect = failure
    client = RoutedLLMClient(cfg)
    client.adapter("default")._client = sdk
    events = []
    client.set_event_sink(
        lambda event, **data: (
            events.append({"event": event, **data}) if event.startswith("llm_retry_") else None
        )
    )

    with pytest.raises(ClientError):
        client.complete([{"role": "user", "content": "test"}], operation="translation.body")

    assert sdk.models.generate_content.call_count == 1
    assert events == []


def test_gemini_client_json_mode():
    """JSON mode must set response_mime_type and parse the result successfully."""
    cfg = model_config(
        kind="gemini",
        api_key_env="TEST_GEMINI_KEY",
        profiles={"strong": dict(model="gemini-3.6-flash")},
    )

    mock_client_instance = MagicMock()
    mock_response = SimpleNamespace(
        text='{"status": "ok", "result": 123}',
        candidates=[SimpleNamespace(finish_reason="STOP")],
        usage_metadata=None,
    )
    mock_client_instance.models.generate_content.return_value = mock_response

    client = RoutedLLMClient(cfg)
    client.adapter("default")._client = mock_client_instance

    json_res = client.complete_json(
        [{"role": "user", "content": "return json"}], operation="translation.body"
    )
    assert json_res == {"status": "ok", "result": 123}

    config_arg = mock_client_instance.models.generate_content.call_args.kwargs["config"]
    assert config_arg.get("response_mime_type") == "application/json"


def test_gemini_client_safety_block():
    """Test detection of provider safety blocking."""
    cfg = model_config(
        kind="gemini",
        api_key_env="TEST_GEMINI_KEY",
        profiles={"strong": dict(model="gemini-3.6-flash")},
    )

    mock_client_instance = MagicMock()
    mock_response = SimpleNamespace(
        text=None,
        candidates=[SimpleNamespace(finish_reason="SAFETY")],
        usage_metadata=None,
    )
    mock_client_instance.models.generate_content.return_value = mock_response

    client = RoutedLLMClient(cfg)
    client.adapter("default")._client = mock_client_instance

    with pytest.raises(RuntimeError, match="blocked the response"):
        client.complete(
            [{"role": "user", "content": "unsafe content"}], operation="translation.body"
        )


def test_factory_build_client_gemini():
    cfg = Config.from_dict({"llm": {"preset": "gemini"}})
    client = build_client(cfg)
    assert isinstance(client, RoutedLLMClient)
    assert isinstance(client.adapter("default"), GeminiClient)
    with pytest.raises(ValueError, match="Unknown provider"):
        Config.from_dict({"llm": {"preset": "google"}})
