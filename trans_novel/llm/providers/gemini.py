"""Google Gemini API Provider 实现（基于 google-genai 官方 SDK）。"""

from __future__ import annotations

import os
import threading
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from ...config import LLMConfig
from ..base import LLMClient, Messages
from ..json_parser import parse_json_loose
from ..tiers import resolve_tier
from ..usage import UsageSample, make_usage_sample, read_usage_int
from ._openai_compatible import ResolvedTier, resolve_provider_tiers

DEFAULT_API_KEY_ENV = "GEMINI_API_KEY"
FALLBACK_API_KEY_ENV = "GOOGLE_API_KEY"


class GeminiTierOptions(BaseModel):
    """Gemini 档位的专属请求选项。"""

    model_config = ConfigDict(extra="forbid")

    thinking_level: str | None = None
    thinking_budget: int | None = None
    temperature: float | None = None
    max_output_tokens: int | None = None
    extra_body: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_thinking_options(self) -> "GeminiTierOptions":
        """验证 thinking_level 与 thinking_budget 互斥。"""
        if self.thinking_level is not None and self.thinking_budget is not None:
            raise ValueError("thinking_level 与 thinking_budget 互斥，不能同时设置")
        return self


def _default_tiers() -> dict[str, ResolvedTier[GeminiTierOptions]]:
    """返回 Gemini 内置的 strong、cheap、fast 三档默认配置。"""
    return {
        "strong": ResolvedTier(
            model="gemini-3.6-flash",
            options=GeminiTierOptions(),
        ),
        "cheap": ResolvedTier(
            model="gemini-3.6-flash",
            options=GeminiTierOptions(),
        ),
        "fast": ResolvedTier(
            model="gemini-3.6-flash",
            options=GeminiTierOptions(),
        ),
    }


def convert_messages_to_gemini(
    messages: Messages,
) -> tuple[str | None, list[dict[str, Any]]]:
    """把 OpenAI 风格的 messages 转换为 Gemini 的 system_instruction 与 contents 列表。

    - role == "system" 提取并合并为 system_instruction
    - role == "user" 保持为 role="user"
    - role == "assistant" 转换为 role="model"
    """
    system_parts: list[str] = []
    contents: list[dict[str, Any]] = []

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "") or ""

        if role == "system":
            if content.strip():
                system_parts.append(content)
        elif role == "assistant":
            contents.append({
                "role": "model",
                "parts": [{"text": content}],
            })
        else:  # user or others
            contents.append({
                "role": "user",
                "parts": [{"text": content}],
            })

    system_instruction = "\n\n".join(system_parts) if system_parts else None
    return system_instruction, contents


def extract_gemini_usage(usage_metadata: Any) -> UsageSample | None:
    """提取并标准化 Gemini API 的 UsageMetadata。

    包含:
    - prompt_token_count (及 cached_content_token_count)
    - candidates_token_count 与 thoughts_token_count
    - total_token_count
    """
    if usage_metadata is None:
        return None

    prompt_tokens = read_usage_int(usage_metadata, "prompt_token_count")
    completion_tokens = (
        read_usage_int(usage_metadata, "candidates_token_count")
        + read_usage_int(usage_metadata, "thoughts_token_count")
    )
    total_tokens = read_usage_int(usage_metadata, "total_token_count") or (
        prompt_tokens + completion_tokens
    )

    cache_hit_tokens = read_usage_int(usage_metadata, "cached_content_token_count")
    cache_miss_tokens = max(0, prompt_tokens - cache_hit_tokens)

    return make_usage_sample(
        {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        },
        cache_hit_tokens=cache_hit_tokens,
        cache_miss_tokens=cache_miss_tokens,
    )


def is_retryable_gemini_error(exc: Exception) -> bool:
    """判断是否为可重试的 Gemini 错误（429 限流、5xx 服务端错误或网络超时/连接错误）。"""
    status_code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if status_code is not None:
        try:
            code = int(status_code)
            if code == 429 or 500 <= code < 600:
                return True
        except (TypeError, ValueError):
            pass

    # 捕获网络超时或连接错误
    exc_name = type(exc).__name__
    if any(
        kw in exc_name.lower()
        for kw in ("timeout", "connection", "network", "servererror")
    ):
        return True

    return False


def get_api_key_from_env(custom_env: str | None = None) -> tuple[str | None, str]:
    """按照优先级获取 Gemini API Key:
    1. custom_env (如果指定)
    2. GEMINI_API_KEY
    3. GOOGLE_API_KEY
    """
    if custom_env:
        val = os.environ.get(custom_env, "").strip()
        if val:
            return val, custom_env

    val_gemini = os.environ.get(DEFAULT_API_KEY_ENV, "").strip()
    if val_gemini:
        return val_gemini, DEFAULT_API_KEY_ENV

    val_google = os.environ.get(FALLBACK_API_KEY_ENV, "").strip()
    if val_google:
        return val_google, FALLBACK_API_KEY_ENV

    target_env = custom_env or DEFAULT_API_KEY_ENV
    return None, target_env


class GeminiClient(LLMClient):
    """Google Gemini 官方 SDK 客户端包装。"""

    def __init__(self, cfg: LLMConfig) -> None:
        super().__init__()
        self.cfg = cfg
        self.tiers = resolve_provider_tiers(
            cfg.tiers,
            options_type=GeminiTierOptions,
            defaults=_default_tiers(),
        )
        self._client: Any = None
        self._client_lock = threading.Lock()

    def validate_credentials(self) -> None:
        """校验 Gemini API Key 配置。"""
        api_key, target_env = get_api_key_from_env(self.cfg.api_key_env)
        if not api_key:
            raise RuntimeError(
                f"未设置环境变量 {target_env}（或 {FALLBACK_API_KEY_ENV}）"
            )

    def _ensure_client(self) -> Any:
        """惰性创建并校验 google.genai.Client 实例。"""
        with self._client_lock:
            if self._client is None:
                try:
                    from google import genai
                except ImportError as error:
                    raise RuntimeError(
                        "需要 google-genai SDK：pip install google-genai"
                        "（或运行 uv add google-genai）"
                    ) from error

                self.validate_credentials()
                api_key, _ = get_api_key_from_env(self.cfg.api_key_env)

                kwargs: dict[str, Any] = {
                    "api_key": api_key,
                    # LLMConfig.timeout 以秒表示，google-genai HttpOptions
                    # 则要求毫秒。
                    "http_options": {"timeout": self.cfg.timeout * 1000},
                }
                if self.cfg.base_url:
                    kwargs["http_options"].update(
                        {"api_option": "REST", "base_url": self.cfg.base_url}
                    )

                self._client = genai.Client(**kwargs)
        return self._client

    def complete(
        self,
        messages: Messages,
        *,
        tier: str = "strong",
        json_mode: bool = False,
        max_tokens: Optional[int] = None,
        stage: Optional[str] = None,
    ) -> str:
        """调用 Gemini 模型并支持重试、JSON 模式与用量归因。"""
        tier_config: ResolvedTier[GeminiTierOptions] = resolve_tier(self.tiers, tier)
        client = self._ensure_client()

        system_instruction, contents = convert_messages_to_gemini(messages)

        # 构造 GenerateContentConfig 配置
        config_kwargs: dict[str, Any] = {}
        if system_instruction:
            config_kwargs["system_instruction"] = system_instruction

        if json_mode:
            config_kwargs["response_mime_type"] = "application/json"

        # max_output_tokens 处理
        effective_max_tokens = max_tokens or tier_config.options.max_output_tokens
        if effective_max_tokens is not None:
            config_kwargs["max_output_tokens"] = effective_max_tokens

        if tier_config.options.temperature is not None:
            config_kwargs["temperature"] = tier_config.options.temperature

        # thinking 配置处理
        if tier_config.options.thinking_level is not None or tier_config.options.thinking_budget is not None:
            try:
                from google.genai import types
                thinking_kwargs: dict[str, Any] = {}
                if tier_config.options.thinking_level is not None:
                    thinking_kwargs["thinking_level"] = tier_config.options.thinking_level
                if tier_config.options.thinking_budget is not None:
                    thinking_kwargs["thinking_budget"] = tier_config.options.thinking_budget
                config_kwargs["thinking_config"] = types.ThinkingConfig(**thinking_kwargs)
            except (ImportError, AttributeError):  # pragma: no cover
                pass

        if tier_config.options.extra_body:
            config_kwargs.update(tier_config.options.extra_body)

        @retry(
            stop=stop_after_attempt(self.cfg.max_retries + 1),
            wait=wait_exponential(multiplier=1, max=30),
            retry=retry_if_exception(is_retryable_gemini_error),
            reraise=True,
        )
        def _call() -> str:
            response = client.models.generate_content(
                model=tier_config.model,
                contents=contents,
                config=config_kwargs,
            )

            # 统计用量
            sample = extract_gemini_usage(getattr(response, "usage_metadata", None))
            self.usage.record(tier, sample, stage)

            # 检查响应有效性与安全拦截
            candidates = getattr(response, "candidates", None)
            if not candidates:
                raise RuntimeError("Gemini API 未返回任何候选结果 (candidates 为空)")

            candidate = candidates[0]
            finish_reason = str(getattr(candidate, "finish_reason", ""))
            if "SAFETY" in finish_reason.upper() or "BLOCK" in finish_reason.upper():
                raise RuntimeError(f"Gemini API 响应被安全拦截 (finish_reason={finish_reason})")

            text = getattr(response, "text", None)
            if not isinstance(text, str):
                # 尝试从 parts 读取
                content = getattr(candidate, "content", None)
                parts = getattr(content, "parts", []) if content else []
                parts_text = [
                    str(getattr(p, "text", ""))
                    for p in parts
                    if getattr(p, "text", None) is not None
                ]
                text = "".join(parts_text) if parts_text else ""

            return text or ""

        return _call()

    def complete_json(
        self,
        messages: Messages,
        *,
        tier: str = "strong",
        max_tokens: Optional[int] = None,
        stage: Optional[str] = None,
    ) -> Any:
        """请求 Gemini 输出 JSON 并使用 parse_json_loose 容错解析。"""
        text = self.complete(
            messages,
            tier=tier,
            json_mode=True,
            max_tokens=max_tokens,
            stage=stage,
        )
        return parse_json_loose(text)
