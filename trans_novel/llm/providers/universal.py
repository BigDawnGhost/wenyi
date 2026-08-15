"""Anthropic Messages 与 OpenAI Chat Completions 共用的 LLM provider。"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from ...config import (
    APIFormat,
    JSONResponseFallback,
    LLMConfig,
    MaxTokensField,
    TierConfig,
    validate_llm_tier,
)
from ..base import LLMClient, Messages
from ..retrying import EmptyResponseError, RetryReporter, provider_retry
from ..usage import UsageSample, make_usage_sample, read_usage_int, read_usage_value

ANTHROPIC_DEFAULT_MAX_TOKENS = 8192
_JSON_MODE_INSTRUCTION = "Output must be valid json."
_COMMON_RESERVED_OVERRIDE_FIELDS = {
    "api_key",
    "api_key_env",
    "base_url",
    "max_completion_tokens",
    "max_tokens",
    "messages",
    "model",
    "stream",
}


@dataclass(frozen=True)
class ResolvedRequestConfig:
    """全局配置与单档精确覆盖合并后的单次请求设置。"""

    model: str
    max_tokens: int | None
    max_tokens_field: MaxTokensField
    temperature: float | None
    thinking: bool | None
    reasoning_effort: str | None
    json_response_fallback: JSONResponseFallback
    request_overrides: dict[str, Any]


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """递归合并请求配置；override 的值优先。"""
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def _override(value: Any, fallback: Any) -> Any:
    """档位字段仅在显式非 None 时覆盖全局值。"""
    return fallback if value is None else value


def resolve_request_config(cfg: LLMConfig, tier: str) -> ResolvedRequestConfig:
    """解析当前档位；未配置的档位直接使用全局模型和参数。"""
    validate_llm_tier(tier)
    override = cfg.tiers.get(tier)
    if override is None:
        override = TierConfig()
    model = _override(override.model, cfg.model)
    return ResolvedRequestConfig(
        model=str(model or "").strip(),
        max_tokens=_override(override.max_tokens, cfg.max_tokens),
        max_tokens_field=_override(override.max_tokens_field, cfg.max_tokens_field),
        temperature=_override(override.temperature, cfg.temperature),
        thinking=_override(override.thinking, cfg.thinking),
        reasoning_effort=_override(override.reasoning_effort, cfg.reasoning_effort),
        json_response_fallback=_override(
            override.json_response_fallback,
            cfg.json_response_fallback,
        ),
        request_overrides=deep_merge(cfg.request_overrides, override.request_overrides),
    )


def normalize_base_url(value: str, api_format: APIFormat) -> str:
    """接受 SDK 基础地址或标准完整操作地址，并返回 SDK 基础地址。"""
    raw = value.strip().rstrip("/")
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("llm.base_url 必须是有效的 HTTP(S) URL")
    if parsed.query or parsed.fragment:
        raise ValueError("llm.base_url 不允许包含 query 或 fragment")

    path = parsed.path.rstrip("/")
    suffixes = ("/chat/completions",) if api_format == "openai" else ("/v1/messages", "/v1")
    for suffix in suffixes:
        if path.endswith(suffix):
            path = path[: -len(suffix)].rstrip("/")
            break
    normalized = urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
    return normalized.rstrip("/")


def _json_messages(messages: Messages) -> Messages:
    """克隆消息并同时在 system 与最后一条 user 中声明 JSON 输出。"""
    request_messages = [dict(message) for message in messages]
    for message in request_messages:
        if message.get("role") == "system":
            content = str(message.get("content", ""))
            if "json" not in content.lower():
                message["content"] = f"{content}\n\n{_JSON_MODE_INSTRUCTION}".strip()
            break
    else:
        request_messages.insert(0, {"role": "system", "content": _JSON_MODE_INSTRUCTION})

    for message in reversed(request_messages):
        if message.get("role") == "user":
            content = str(message.get("content", ""))
            if "json" not in content.lower():
                message["content"] = f"{content}\n\n{_JSON_MODE_INSTRUCTION}".strip()
            break
    return request_messages


def convert_messages_to_anthropic(messages: Messages) -> tuple[str | None, Messages]:
    """把 system 消息提升到 Anthropic 顶层，其余角色保持对话顺序。"""
    system_parts: list[str] = []
    converted: Messages = []
    for message in messages:
        role = message.get("role", "user")
        content = str(message.get("content", "") or "")
        if role == "system":
            if content.strip():
                system_parts.append(content)
            continue
        converted.append(
            {
                "role": "assistant" if role == "assistant" else "user",
                "content": content,
            }
        )
    return ("\n\n".join(system_parts) or None), converted


def normalize_openai_usage(usage: Any) -> UsageSample | None:
    """把 OpenAI 缓存明细转换为统一用量。"""
    if usage is None:
        return None
    details = read_usage_value(usage, "prompt_tokens_details")
    cached_value = read_usage_value(details, "cached_tokens")
    prompt_tokens = read_usage_int(usage, "prompt_tokens")
    cache_hit_tokens = read_usage_int(details, "cached_tokens") if cached_value is not None else 0
    return make_usage_sample(
        usage,
        cache_hit_tokens=cache_hit_tokens,
        cache_miss_tokens=(
            max(0, prompt_tokens - cache_hit_tokens) if cached_value is not None else 0
        ),
    )


def normalize_anthropic_usage(usage: Any) -> UsageSample | None:
    """按 Anthropic 计费定义汇总普通、缓存写入与缓存读取 token。"""
    if usage is None:
        return None
    uncached = read_usage_int(usage, "input_tokens")
    cache_creation = read_usage_int(usage, "cache_creation_input_tokens")
    cache_read = read_usage_int(usage, "cache_read_input_tokens")
    output = read_usage_int(usage, "output_tokens")
    prompt = uncached + cache_creation + cache_read
    return UsageSample(
        prompt_tokens=prompt,
        completion_tokens=output,
        total_tokens=prompt + output,
        cache_hit_tokens=cache_read,
        cache_miss_tokens=uncached + cache_creation,
    )


def _text_parts(value: Any) -> list[str]:
    """从 SDK 文本块列表中提取最终文本，忽略 thinking/tool 等块。"""
    if isinstance(value, str):
        return [value]
    if not isinstance(value, (list, tuple)):
        return []
    parts: list[str] = []
    for block in value:
        block_type = getattr(block, "type", None)
        if block_type is None and isinstance(block, dict):
            block_type = block.get("type")
        if block_type != "text":
            continue
        text = getattr(block, "text", None)
        if text is None and isinstance(block, dict):
            text = block.get("text")
        if isinstance(text, str):
            parts.append(text)
    return parts


def extract_openai_text(
    response: Any,
    *,
    json_mode: bool = False,
    json_response_fallback: JSONResponseFallback = "none",
    provider_name: str = "OpenAI",
) -> str:
    """读取首个 Chat Completions 文本，并处理显式启用的 JSON 备用字段。"""
    choices = read_usage_value(response, "choices")
    if not choices:
        raise RuntimeError("OpenAI 格式 API 未返回 choices")
    choice = choices[0]
    message = read_usage_value(choice, "message")
    content = "".join(_text_parts(read_usage_value(message, "content")))
    if content.strip():
        return content
    if str(read_usage_value(choice, "finish_reason") or "").lower() == "length":
        raise RuntimeError(f"{provider_name} 响应因达到 token 上限而截断")

    fallback = None
    if json_mode and json_response_fallback == "reasoning_content":
        value = read_usage_value(message, "reasoning_content")
        fallback = value if isinstance(value, str) else None
    if fallback is None or not fallback.strip():
        raise EmptyResponseError(f"{provider_name} 响应的 content 为空")
    try:
        json.loads(fallback)
    except json.JSONDecodeError as error:
        raise EmptyResponseError(f"{provider_name} 配置的 JSON 备用响应不是合法 JSON") from error
    return fallback


def extract_anthropic_text(response: Any) -> str:
    """拼接 Anthropic 响应中的全部 text block。"""
    return "".join(_text_parts(getattr(response, "content", None)))


class UniversalClient(LLMClient):
    """在一个实现中封装 Anthropic Messages 与 OpenAI Chat Completions。"""

    def __init__(self, cfg: LLMConfig) -> None:
        super().__init__()
        if cfg.api_format == "fake":
            raise ValueError("api_format: fake 应由 FakeClient 处理")
        self.cfg = cfg
        self.api_format: APIFormat = cfg.api_format
        self.provider_name = "Anthropic" if cfg.api_format == "anthropic" else "OpenAI"
        self._validate_required_configuration()
        self.base_url = normalize_base_url(str(cfg.base_url), cfg.api_format)
        self._validate_all_overrides()
        self._client: Any = None
        self._client_lock = threading.Lock()

    def _validate_required_configuration(self) -> None:
        """在构建客户端时一次报告所有缺失的运行必填项。"""
        missing: list[str] = []
        direct_key = self.cfg.api_key.get_secret_value().strip() if self.cfg.api_key else ""
        if not direct_key and not str(self.cfg.api_key_env or "").strip():
            missing.append("llm.api_key 或 llm.api_key_env")
        if not str(self.cfg.base_url or "").strip():
            missing.append("llm.base_url")
        if not str(self.cfg.model or "").strip():
            missing.append("llm.model")
        if missing:
            raise ValueError("LLM 配置缺少必填项：" + "、".join(missing))

    def _validate_all_overrides(self) -> None:
        """禁止原始透传覆盖由客户端维护的结构字段。"""
        candidates = [("llm.request_overrides", self.cfg.request_overrides)]
        candidates.extend(
            (f"llm.tiers.{name}.request_overrides", tier.request_overrides)
            for name, tier in self.cfg.tiers.items()
        )
        reserved = set(_COMMON_RESERVED_OVERRIDE_FIELDS)
        if self.api_format == "anthropic":
            reserved.update(("response_format", "system"))
        for location, values in candidates:
            collisions = sorted(reserved.intersection(values))
            if collisions:
                raise ValueError(f"{location} 不允许覆盖保留字段：" + "、".join(collisions))
        if self.api_format == "anthropic":
            if self.cfg.max_tokens_field != "max_tokens" or any(
                tier.max_tokens_field not in (None, "max_tokens")
                for tier in self.cfg.tiers.values()
            ):
                raise ValueError("Anthropic 格式仅支持 max_tokens_field: max_tokens")

    def _api_key(self) -> str:
        """按直接值优先、环境变量回退的规则解析 API Key。"""
        direct = self.cfg.api_key.get_secret_value().strip() if self.cfg.api_key else ""
        if direct:
            return direct
        env_name = str(self.cfg.api_key_env or "").strip()
        value = os.environ.get(env_name, "").strip() if env_name else ""
        if value:
            return value
        if env_name:
            raise RuntimeError(f"未设置环境变量 {env_name}（{self.provider_name} API key）")
        raise RuntimeError("需要配置 llm.api_key 或 llm.api_key_env")

    def validate_credentials(self) -> None:
        """在任何模型流程开始前验证可用密钥，且绝不回显密钥值。"""
        self._api_key()

    def _ensure_client(self) -> Any:
        """线程安全地延迟创建所选格式的官方 SDK 客户端。"""
        with self._client_lock:
            if self._client is not None:
                return self._client
            api_key = self._api_key()
            if self.api_format == "openai":
                try:
                    from openai import OpenAI
                except ImportError as error:  # pragma: no cover
                    raise RuntimeError("需要 openai SDK：pip install openai") from error
                self._client = OpenAI(
                    api_key=api_key,
                    base_url=self.base_url,
                    timeout=self.cfg.timeout,
                    max_retries=0,
                )
            else:
                try:
                    from anthropic import Anthropic
                except ImportError as error:  # pragma: no cover
                    raise RuntimeError("需要 anthropic SDK：pip install anthropic") from error
                self._client = Anthropic(
                    api_key=api_key,
                    base_url=self.base_url,
                    timeout=self.cfg.timeout,
                    max_retries=0,
                )
            return self._client

    @staticmethod
    def _common_optional_fields(settings: ResolvedRequestConfig) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if settings.temperature is not None:
            body["temperature"] = settings.temperature
        return body

    @staticmethod
    def _promote_sdk_fields(
        kwargs: dict[str, Any],
        body: dict[str, Any],
        names: tuple[str, ...],
    ) -> None:
        """把官方 SDK 已声明的字段提升为显式参数，仅透传其余厂商扩展。"""
        for name in names:
            if name in body:
                kwargs[name] = body.pop(name)
        if body:
            kwargs["extra_body"] = body

    def _build_openai_kwargs(
        self,
        settings: ResolvedRequestConfig,
        messages: Messages,
        *,
        json_mode: bool,
        max_tokens: int | None,
    ) -> dict[str, Any]:
        request_messages = _json_messages(messages) if json_mode else [dict(m) for m in messages]
        kwargs: dict[str, Any] = {
            "model": settings.model,
            "messages": request_messages,
            "stream": False,
        }
        effective_max_tokens = max_tokens if max_tokens is not None else settings.max_tokens
        if effective_max_tokens is not None:
            kwargs[settings.max_tokens_field] = effective_max_tokens
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        generated = self._common_optional_fields(settings)
        effort = str(settings.reasoning_effort or "").strip()
        if settings.thinking is True:
            generated["reasoning_effort"] = effort or "high"
        elif settings.thinking is False:
            generated["reasoning_effort"] = "none"
        elif effort:
            generated["reasoning_effort"] = effort
        extra_body = deep_merge(generated, settings.request_overrides)
        if json_mode:
            extra_body.pop("response_format", None)
        self._promote_sdk_fields(
            kwargs,
            extra_body,
            ("temperature", "reasoning_effort"),
        )
        return kwargs

    def _build_anthropic_kwargs(
        self,
        settings: ResolvedRequestConfig,
        messages: Messages,
        *,
        json_mode: bool,
        max_tokens: int | None,
    ) -> dict[str, Any]:
        request_messages = _json_messages(messages) if json_mode else [dict(m) for m in messages]
        system, converted = convert_messages_to_anthropic(request_messages)
        kwargs: dict[str, Any] = {
            "model": settings.model,
            "messages": converted,
            "stream": False,
            "max_tokens": (
                max_tokens
                if max_tokens is not None
                else settings.max_tokens or ANTHROPIC_DEFAULT_MAX_TOKENS
            ),
        }
        if system:
            kwargs["system"] = system

        generated = self._common_optional_fields(settings)
        if settings.thinking is True:
            generated["thinking"] = {"type": "adaptive"}
        elif settings.thinking is False:
            generated["thinking"] = {"type": "disabled"}
        effort = str(settings.reasoning_effort or "").strip()
        if effort:
            generated["output_config"] = {"effort": effort}
        extra_body = deep_merge(generated, settings.request_overrides)
        self._promote_sdk_fields(
            kwargs,
            extra_body,
            ("temperature", "thinking", "output_config"),
        )
        return kwargs

    def complete(
        self,
        messages: Messages,
        *,
        tier: str = "strong",
        json_mode: bool = False,
        max_tokens: int | None = None,
        stage: str | None = None,
    ) -> str:
        """按选定协议请求纯文本，并统一处理重试、活动事件与用量。"""
        with self.request_activity(stage=stage, tier=tier) as request_id:
            settings = resolve_request_config(self.cfg, tier)
            if not settings.model:
                raise ValueError(f"llm.tiers.{tier}.model 不能为空")
            if self.api_format == "openai":
                kwargs = self._build_openai_kwargs(
                    settings,
                    messages,
                    json_mode=json_mode,
                    max_tokens=max_tokens,
                )
            else:
                kwargs = self._build_anthropic_kwargs(
                    settings,
                    messages,
                    json_mode=json_mode,
                    max_tokens=max_tokens,
                )
            client = self._ensure_client()
            reporter = RetryReporter(
                provider=self.provider_name,
                tier=tier,
                stage=stage,
                max_attempts=max(1, self.cfg.max_retries + 1),
                emit=self._emit_event,
                activity_emit=lambda event, **data: self._emit_activity(
                    event,
                    request_id=request_id,
                    **data,
                ),
            )

            @provider_retry(self.cfg.max_retries, reporter)
            def _call() -> str:
                if self.api_format == "openai":
                    response = client.chat.completions.create(**kwargs)
                    sample = normalize_openai_usage(getattr(response, "usage", None))
                    self.usage.record(tier, sample, stage)
                    return extract_openai_text(
                        response,
                        json_mode=json_mode,
                        json_response_fallback=settings.json_response_fallback,
                        provider_name=self.provider_name,
                    )
                else:
                    response = client.messages.create(**kwargs)
                    sample = normalize_anthropic_usage(getattr(response, "usage", None))
                self.usage.record(tier, sample, stage)
                return extract_anthropic_text(response)

            return _call()


__all__ = [
    "ANTHROPIC_DEFAULT_MAX_TOKENS",
    "ResolvedRequestConfig",
    "UniversalClient",
    "convert_messages_to_anthropic",
    "deep_merge",
    "extract_anthropic_text",
    "extract_openai_text",
    "normalize_anthropic_usage",
    "normalize_base_url",
    "normalize_openai_usage",
    "resolve_request_config",
]
