"""tests/test_llm_gemini.py - Gemini LLM Provider 完整单元测试"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from trans_novel.config import Config, LLMConfig, TierConfig
from trans_novel.llm.factory import build_client
from trans_novel.llm.providers.gemini import (
    GeminiClient,
    GeminiTierOptions,
    convert_messages_to_gemini,
    extract_gemini_usage,
    get_api_key_from_env,
    is_retryable_gemini_error,
)


def test_gemini_tier_options_thinking_mutual_exclusion():
    """测试 thinking_level 与 thinking_budget 的互斥校验。"""
    opt1 = GeminiTierOptions(thinking_level="high")
    assert opt1.thinking_level == "high"

    opt2 = GeminiTierOptions(thinking_budget=1024)
    assert opt2.thinking_budget == 1024

    with pytest.raises(ValidationError, match="thinking_level 与 thinking_budget 互斥"):
        GeminiTierOptions(thinking_level="high", thinking_budget=1024)


def test_api_key_env_precedence():
    """测试 API Key 获取的环境变量优先级与退避规则。"""
    with patch.dict(os.environ, {"CUSTOM_KEY": "custom_val", "GEMINI_API_KEY": "gemini_val", "GOOGLE_API_KEY": "google_val"}, clear=True):
        key, env_name = get_api_key_from_env("CUSTOM_KEY")
        assert key == "custom_val"
        assert env_name == "CUSTOM_KEY"

    with patch.dict(os.environ, {"GEMINI_API_KEY": "gemini_val", "GOOGLE_API_KEY": "google_val"}, clear=True):
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
    """测试 OpenAI 格式转换到 Gemini 格式。"""
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
    """测试 Gemini Token 用量与缓存 Token 提取计算。"""
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


def test_is_retryable_gemini_error():
    """测试重试逻辑异常过滤器。"""
    err_429 = SimpleNamespace(code=429)
    assert is_retryable_gemini_error(err_429) is True

    err_503 = SimpleNamespace(code=503)
    assert is_retryable_gemini_error(err_503) is True

    err_400 = SimpleNamespace(code=400)
    assert is_retryable_gemini_error(err_400) is False

    class TimeoutError(Exception):
        pass

    assert is_retryable_gemini_error(TimeoutError("Connection timeout")) is True


def test_gemini_client_validate_credentials():
    """测试客户端凭据校验。"""
    cfg = LLMConfig(provider="gemini", api_key_env="TEST_MISSING_ENV_KEY")
    client = GeminiClient(cfg)

    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(RuntimeError, match="未设置环境变量"):
            client.validate_credentials()

    with patch.dict(os.environ, {"TEST_MISSING_ENV_KEY": "valid_key"}):
        client.validate_credentials()


def test_gemini_client_applies_timeout_in_milliseconds():
    """通用秒级 timeout 应转换为 google-genai 使用的毫秒值。"""
    cfg = LLMConfig(
        provider="gemini",
        api_key_env="TEST_GEMINI_KEY",
        timeout=17,
    )

    with (
        patch.dict(os.environ, {"TEST_GEMINI_KEY": "valid_key"}, clear=True),
        patch("google.genai.Client") as client_type,
    ):
        GeminiClient(cfg)._ensure_client()

    client_type.assert_called_once_with(
        api_key="valid_key",
        http_options={"timeout": 17_000},
    )


def test_gemini_client_complete_and_usage():
    """测试 GeminiClient.complete 流程与用量归因。"""
    cfg = LLMConfig(
        provider="gemini",
        api_key_env="TEST_GEMINI_KEY",
        tiers={
            "strong": TierConfig(model="gemini-3.6-flash", options={"temperature": 0.3})
        },
    )

    mock_client_instance = MagicMock()
    mock_response = SimpleNamespace(
        text="翻译结果测试",
        candidates=[SimpleNamespace(finish_reason="STOP", content=SimpleNamespace(parts=[SimpleNamespace(text="翻译结果测试")]))],
        usage_metadata=SimpleNamespace(
            prompt_token_count=80,
            candidates_token_count=20,
            thoughts_token_count=5,
            total_token_count=105,
            cached_content_token_count=0,
        ),
    )
    mock_client_instance.models.generate_content.return_value = mock_response

    client = GeminiClient(cfg)
    client._client = mock_client_instance

    res = client.complete([{"role": "user", "content": "test"}], stage="translation")

    assert res == "翻译结果测试"
    mock_client_instance.models.generate_content.assert_called_once()
    call_kwargs = mock_client_instance.models.generate_content.call_args.kwargs
    assert call_kwargs["model"] == "gemini-3.6-flash"
    assert call_kwargs["config"]["temperature"] == 0.3

    summary = client.usage_summary()
    assert summary["totals"]["prompt_tokens"] == 80
    assert summary["totals"]["completion_tokens"] == 25


def test_gemini_client_json_mode():
    """测试 json_mode=True 时触发 response_mime_type 并成功解析。"""
    cfg = LLMConfig(
        provider="gemini",
        api_key_env="TEST_GEMINI_KEY",
        tiers={"strong": TierConfig(model="gemini-3.6-flash")},
    )

    mock_client_instance = MagicMock()
    mock_response = SimpleNamespace(
        text='{"status": "ok", "result": 123}',
        candidates=[SimpleNamespace(finish_reason="STOP")],
        usage_metadata=None,
    )
    mock_client_instance.models.generate_content.return_value = mock_response

    client = GeminiClient(cfg)
    client._client = mock_client_instance

    json_res = client.complete_json([{"role": "user", "content": "return json"}])
    assert json_res == {"status": "ok", "result": 123}

    config_arg = mock_client_instance.models.generate_content.call_args.kwargs["config"]
    assert config_arg.get("response_mime_type") == "application/json"


def test_gemini_client_safety_block():
    """测试安全策略拦截捕获。"""
    cfg = LLMConfig(
        provider="gemini",
        api_key_env="TEST_GEMINI_KEY",
        tiers={"strong": TierConfig(model="gemini-3.6-flash")},
    )

    mock_client_instance = MagicMock()
    mock_response = SimpleNamespace(
        text=None,
        candidates=[SimpleNamespace(finish_reason="SAFETY")],
        usage_metadata=None,
    )
    mock_client_instance.models.generate_content.return_value = mock_response

    client = GeminiClient(cfg)
    client._client = mock_client_instance

    with pytest.raises(RuntimeError, match="安全拦截"):
        client.complete([{"role": "user", "content": "unsafe content"}])


def test_factory_build_client_gemini():
    """测试 factory build_client 工厂函数对 gemini 和 google 的路由构建。"""
    raw_config = {
        "llm": {
            "provider": "gemini",
            "api_key_env": "TEST_KEY",
            "tiers": {"strong": {"model": "gemini-3.6-flash"}},
        }
    }
    cfg = Config.from_dict(raw_config)
    client = build_client(cfg)
    assert isinstance(client, GeminiClient)

    raw_config_google = {
        "llm": {
            "provider": "google",
            "api_key_env": "TEST_KEY",
            "tiers": {"strong": {"model": "gemini-3.6-flash"}},
        }
    }
    cfg_google = Config.from_dict(raw_config_google)
    client_google = build_client(cfg_google)
    assert isinstance(client_google, GeminiClient)
