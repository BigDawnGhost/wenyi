"""OpenAI 兼容 provider 共用的传输、重试与档位解析。"""

from __future__ import annotations

import os
import threading
from abc import abstractmethod
from dataclasses import dataclass
from typing import Any, Generic, Optional, TypeVar

from pydantic import BaseModel
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ...config import LLMConfig, TierConfig
from ..base import LLMClient, Messages
from ..tiers import resolve_tier
from ..usage import (
    UsageSample,
    make_usage_sample,
    read_usage_int,
    read_usage_value,
)

OptionsT = TypeVar("OptionsT", bound=BaseModel)
_JSON_MODE_INSTRUCTION = "Output must be valid json."


@dataclass(frozen=True)
class ResolvedTier(Generic[OptionsT]):
    """provider 已补全并校验的运行时档位。"""

    model: str
    options: OptionsT


def resolve_provider_tiers(
    overrides: dict[str, TierConfig],
    *,
    options_type: type[OptionsT],
    defaults: dict[str, ResolvedTier[OptionsT]] | None = None,
) -> dict[str, ResolvedTier[OptionsT]]:
    """合并通用档位覆盖，并交给 provider 专属 options 模型校验。"""
    tiers = dict(defaults or {})
    for name, override in overrides.items():
        current = tiers.get(name)
        model = override.model or (current.model if current else None)
        if not model:
            raise ValueError(f"llm.tiers.{name}.model 不能为空")
        option_values = current.options.model_dump() if current else {}
        option_values.update(override.options)
        tiers[name] = ResolvedTier(
            model=model,
            options=options_type.model_validate(option_values),
        )
    if "strong" not in tiers:
        raise ValueError("配置缺少 llm.tiers.strong.model")
    return tiers


def base_request_kwargs(
    model: str,
    messages: Messages,
    *,
    json_mode: bool,
) -> dict[str, Any]:
    """构造 Chat Completions 基础参数，并为 JSON 模式补充明确指令。"""
    request_messages = messages
    if json_mode:
        request_messages = [dict(message) for message in messages]
        for message in request_messages:
            if message.get("role") == "system":
                message["content"] = (
                    f"{message.get('content', '')}\n\n{_JSON_MODE_INSTRUCTION}"
                )
                break
        else:
            request_messages.insert(
                0,
                {"role": "system", "content": _JSON_MODE_INSTRUCTION},
            )
        # 有些中转/网关只校验 user 角色内容（例如转发到 Responses API 的
        # text.format 校验只看 input 里的用户内容），只在 system 里提到
        # "json" 未必够，所以也在最后一条 user 消息里补一份，双重保证。
        for message in reversed(request_messages):
            if message.get("role") == "user":
                content = str(message.get("content", ""))
                if "json" not in content.lower():
                    message["content"] = f"{content}\n\n{_JSON_MODE_INSTRUCTION}"
                break
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": request_messages,
        "stream": False,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    return kwargs


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """递归合并 provider 请求体；用户值优先。"""
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def normalize_openai_usage(usage: Any) -> UsageSample | None:
    """把 OpenAI 风格的嵌套缓存明细转换成统一用量。"""
    if usage is None:
        return None
    details = read_usage_value(usage, "prompt_tokens_details")
    cached_value = read_usage_value(details, "cached_tokens")
    if cached_value is None:
        cache_hit_tokens = 0
        cache_miss_tokens = 0
    else:
        cache_hit_tokens = read_usage_int(details, "cached_tokens")
        cache_miss_tokens = max(
            0,
            read_usage_int(usage, "prompt_tokens") - cache_hit_tokens,
        )
    return make_usage_sample(
        usage,
        cache_hit_tokens=cache_hit_tokens,
        cache_miss_tokens=cache_miss_tokens,
    )


class OpenAICompatibleBaseClient(LLMClient, Generic[OptionsT]):
    """所有 OpenAI Chat Completions 兼容 provider 的共用客户端。"""

    def __init__(
        self,
        cfg: LLMConfig,
        *,
        provider_name: str,
        default_base_url: str | None,
        default_api_key_env: str | None,
        tiers: dict[str, ResolvedTier[OptionsT]],
        requires_api_key: bool,
    ) -> None:
        """解析连接信息并保存已校验档位，SDK 客户端稍后按需创建。"""
        super().__init__()
        self.cfg = cfg
        self.provider_name = provider_name
        self.base_url = cfg.base_url or default_base_url
        self.api_key_env = cfg.api_key_env or default_api_key_env
        self.tiers = tiers
        self.requires_api_key = requires_api_key
        if not self.base_url:
            raise ValueError(f"{provider_name} provider 需要配置 llm.base_url")
        self._client: Any = None
        self._client_lock = threading.Lock()

    def _ensure_client(self) -> Any:
        """线程安全地惰性创建 OpenAI SDK 客户端并校验 API Key。"""
        with self._client_lock:
            if self._client is None:
                try:
                    from openai import OpenAI
                except ImportError as error:  # pragma: no cover
                    raise RuntimeError(
                        "需要 openai SDK：pip install openai"
                        "（或把 llm.provider 设为 fake 做离线测试）"
                    ) from error
                api_key = os.environ.get(self.api_key_env) if self.api_key_env else None
                if (self.requires_api_key or self.api_key_env) and not api_key:
                    raise RuntimeError(
                        f"未设置环境变量 {self.api_key_env}（{self.provider_name} API key）"
                    )
                self._client = OpenAI(
                    api_key=api_key or "no-key",
                    base_url=self.base_url,
                    timeout=self.cfg.timeout,
                )
        return self._client

    def _normalize_usage(self, usage: Any) -> UsageSample | None:
        """标准 OpenAI 兼容响应默认使用嵌套缓存明细。"""
        return normalize_openai_usage(usage)

    @abstractmethod
    def _build_request_kwargs(
        self,
        tier_config: ResolvedTier[OptionsT],
        messages: Messages,
        *,
        json_mode: bool,
        max_tokens: Optional[int],
    ) -> dict[str, Any]:
        """把通用调用转换成 provider 的请求方言。"""
        raise NotImplementedError

    def complete(
        self,
        messages: Messages,
        *,
        tier: str = "strong",
        json_mode: bool = False,
        max_tokens: Optional[int] = None,
        stage: Optional[str] = None,
    ) -> str:
        """按指定档位调用兼容接口，自动重试并记录标准化用量。"""
        tier_config = resolve_tier(self.tiers, tier)
        kwargs = self._build_request_kwargs(
            tier_config,
            messages,
            json_mode=json_mode,
            max_tokens=max_tokens,
        )
        client = self._ensure_client()

        @retry(
            stop=stop_after_attempt(self.cfg.max_retries + 1),
            wait=wait_exponential(multiplier=1, max=30),
            retry=retry_if_exception_type(Exception),
            reraise=True,
        )
        def _call() -> str:
            """执行一次实际请求；异常交由 tenacity 重试装饰器处理。"""
            response = client.chat.completions.create(**kwargs)
            sample = self._normalize_usage(getattr(response, "usage", None))
            self.usage.record(tier, sample, stage)
            return response.choices[0].message.content or ""

        return _call()
