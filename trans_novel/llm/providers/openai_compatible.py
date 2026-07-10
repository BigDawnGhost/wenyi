"""任意 OpenAI Chat Completions 兼容端点。"""

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


class OpenAICompatibleTierOptions(BaseModel):
    """通用兼容端点选项；私有请求字段放在 extra_body。"""

    model_config = ConfigDict(extra="forbid")

    thinking: bool = False
    extra_body: dict[str, Any] = Field(default_factory=dict)


def build_request_kwargs(
    tier_config: ResolvedTier[OpenAICompatibleTierOptions],
    messages: Messages,
    *,
    json_mode: bool = False,
    max_tokens: Optional[int] = None,
) -> dict[str, Any]:
    kwargs = base_request_kwargs(tier_config.model, messages, json_mode=json_mode)
    if tier_config.options.extra_body:
        kwargs["extra_body"] = deep_merge({}, tier_config.options.extra_body)
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
        )
