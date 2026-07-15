"""任意 OpenAI Chat Completions 兼容端点及其思考参数方言。"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from ...config import LLMConfig, ReasoningStyle
from ..base import Messages
from ._openai_compatible import (
    OpenAICompatibleBaseClient,
    ResolvedTier,
    base_request_kwargs,
    deep_merge,
    resolve_provider_tiers,
)


class OpenAICompatibleTierOptions(BaseModel):
    """通用兼容端点选项；未知字段通过 request_overrides 透传。"""

    model_config = ConfigDict(extra="forbid")

    thinking: bool = False
    reasoning_effort: str = "high"
    request_overrides: dict[str, Any] = Field(default_factory=dict)


def _reasoning_body(
    options: OpenAICompatibleTierOptions,
    reasoning_style: ReasoningStyle,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """返回 SDK 参数和需要作为原始请求体透传的方言字段。"""
    kwargs: dict[str, Any] = {}
    extra_body: dict[str, Any] = {}
    if reasoning_style == "deepseek":
        extra_body["thinking"] = {
            "type": "enabled" if options.thinking else "disabled"
        }
        if options.thinking:
            kwargs["reasoning_effort"] = options.reasoning_effort
    elif reasoning_style == "openai":
        kwargs["reasoning_effort"] = (
            options.reasoning_effort if options.thinking else "none"
        )
    elif reasoning_style == "openrouter":
        extra_body["reasoning"] = (
            {"effort": options.reasoning_effort}
            if options.thinking
            else {"enabled": False}
        )
    return kwargs, extra_body


def build_request_kwargs(
    tier_config: ResolvedTier[OpenAICompatibleTierOptions],
    messages: Messages,
    *,
    json_mode: bool = False,
    max_tokens: Optional[int] = None,
    reasoning_style: ReasoningStyle = "none",
) -> dict[str, Any]:
    kwargs = base_request_kwargs(tier_config.model, messages, json_mode=json_mode)
    reasoning_kwargs, extra_body = _reasoning_body(
        tier_config.options,
        reasoning_style,
    )
    kwargs.update(reasoning_kwargs)
    if tier_config.options.request_overrides:
        extra_body = deep_merge(
            extra_body,
            tier_config.options.request_overrides,
        )
    if extra_body:
        kwargs["extra_body"] = extra_body
    if max_tokens is not None:
        kwargs["max_tokens"] = (
            max(max_tokens, 4096) if tier_config.options.thinking else max_tokens
        )
    return kwargs


class OpenAICompatibleClient(
    OpenAICompatibleBaseClient[OpenAICompatibleTierOptions]
):
    def __init__(
        self,
        cfg: LLMConfig,
        *,
        provider_name: str = "OpenAI-compatible",
        default_base_url: str | None = None,
        default_api_key_env: str | None = None,
        requires_api_key: bool = False,
    ) -> None:
        tiers = resolve_provider_tiers(
            cfg.tiers,
            options_type=OpenAICompatibleTierOptions,
        )
        self.reasoning_style: ReasoningStyle = cfg.reasoning_style
        super().__init__(
            cfg,
            provider_name=provider_name,
            default_base_url=default_base_url,
            default_api_key_env=default_api_key_env,
            tiers=tiers,
            requires_api_key=requires_api_key,
        )

    def _build_request_kwargs(
        self,
        tier_config: ResolvedTier[OpenAICompatibleTierOptions],
        messages: Messages,
        *,
        json_mode: bool,
        max_tokens: Optional[int],
    ) -> dict[str, Any]:
        return build_request_kwargs(
            tier_config,
            messages,
            json_mode=json_mode,
            max_tokens=max_tokens,
            reasoning_style=self.reasoning_style,
        )
