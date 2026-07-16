"""通过 OpenRouter 的 OpenAI 兼容接口调用模型。"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from ...config import LLMConfig
from ..base import Messages
from ._openai_compatible import (
    OpenAICompatibleBaseClient,
    ResolvedTier,
    base_request_kwargs,
    deep_merge,
    resolve_provider_tiers,
)

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_API_KEY_ENV = "OPENROUTER_API_KEY"


class OpenRouterTierOptions(BaseModel):
    """OpenRouter 档位的专属请求选项。"""

    model_config = ConfigDict(extra="forbid")

    thinking: bool = True
    reasoning_effort: str = "high"
    extra_body: dict[str, Any] = Field(default_factory=dict)


def build_request_kwargs(
    tier_config: ResolvedTier[OpenRouterTierOptions],
    messages: Messages,
    *,
    json_mode: bool = False,
    max_tokens: Optional[int] = None,
) -> dict[str, Any]:
    """把档位选项转换成 OpenRouter reasoning 请求体。"""
    kwargs = base_request_kwargs(tier_config.model, messages, json_mode=json_mode)
    extra_body = {
        "reasoning": (
            {"effort": tier_config.options.reasoning_effort}
            if tier_config.options.thinking
            else {"enabled": False}
        )
    }
    if tier_config.options.extra_body:
        extra_body = deep_merge(extra_body, tier_config.options.extra_body)
    kwargs["extra_body"] = extra_body
    if max_tokens is not None:
        kwargs["max_tokens"] = (
            max(max_tokens, 4096) if tier_config.options.thinking else max_tokens
        )
    return kwargs


class OpenRouterClient(OpenAICompatibleBaseClient[OpenRouterTierOptions]):
    def __init__(self, cfg: LLMConfig):
        """校验 OpenRouter 档位选项并初始化官方兼容端点。"""
        tiers = resolve_provider_tiers(
            cfg.tiers,
            options_type=OpenRouterTierOptions,
        )
        super().__init__(
            cfg,
            provider_name="OpenRouter",
            default_base_url=DEFAULT_BASE_URL,
            default_api_key_env=DEFAULT_API_KEY_ENV,
            tiers=tiers,
            requires_api_key=True,
        )

    def _build_request_kwargs(
        self,
        tier_config: ResolvedTier[OpenRouterTierOptions],
        messages: Messages,
        *,
        json_mode: bool,
        max_tokens: Optional[int],
    ) -> dict[str, Any]:
        """构造当前 OpenRouter 档位的最终请求参数。"""
        return build_request_kwargs(
            tier_config,
            messages,
            json_mode=json_mode,
            max_tokens=max_tokens,
        )
