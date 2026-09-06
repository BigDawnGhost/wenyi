"""Call models through the official OpenAI Chat Completions endpoint."""

from __future__ import annotations

from typing import Any

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

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_API_KEY_ENV = "OPENAI_API_KEY"


class OpenAITierOptions(BaseModel):
    """OpenAI-specific tier request options."""

    model_config = ConfigDict(extra="forbid")

    thinking: bool = True
    reasoning_effort: str = "high"
    extra_body: dict[str, Any] = Field(default_factory=dict)


def build_request_kwargs(
    tier_config: ResolvedTier[OpenAITierOptions],
    messages: Messages,
    *,
    json_mode: bool = False,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    """Build OpenAI request arguments and limit output with max_completion_tokens."""
    kwargs = base_request_kwargs(tier_config.model, messages, json_mode=json_mode)
    kwargs["reasoning_effort"] = (
        tier_config.options.reasoning_effort if tier_config.options.thinking else "none"
    )
    if tier_config.options.extra_body:
        kwargs["extra_body"] = deep_merge({}, tier_config.options.extra_body)
    if max_tokens is not None:
        kwargs["max_completion_tokens"] = (
            max(max_tokens, 4096) if tier_config.options.thinking else max_tokens
        )
    return kwargs


class OpenAIClient(OpenAICompatibleBaseClient[OpenAITierOptions]):
    def __init__(self, cfg: LLMConfig):
        """Validate OpenAI tier options and initialize the official endpoint client."""
        tiers = resolve_provider_tiers(
            cfg.tiers,
            options_type=OpenAITierOptions,
        )
        super().__init__(
            cfg,
            provider_name="OpenAI",
            default_base_url=DEFAULT_BASE_URL,
            default_api_key_env=DEFAULT_API_KEY_ENV,
            tiers=tiers,
            requires_api_key=True,
        )

    def _build_request_kwargs(
        self,
        tier_config: ResolvedTier[OpenAITierOptions],
        messages: Messages,
        *,
        json_mode: bool,
        max_tokens: int | None,
    ) -> dict[str, Any]:
        """Build final request arguments for the selected OpenAI tier."""
        return build_request_kwargs(
            tier_config,
            messages,
            json_mode=json_mode,
            max_tokens=max_tokens,
        )
