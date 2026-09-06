"""Call DeepSeek through its native OpenAI-compatible endpoint."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ...config import LLMConfig
from ..base import Messages
from ..usage import UsageSample, make_usage_sample, read_usage_int
from ._openai_compatible import (
    OpenAICompatibleBaseClient,
    ResolvedTier,
    base_request_kwargs,
    deep_merge,
    resolve_provider_tiers,
)

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_API_KEY_ENV = "DEEPSEEK_API_KEY"


def normalize_deepseek_usage(usage: Any) -> UsageSample | None:
    """Normalize DeepSeek's top-level cache counters into shared usage accounting."""
    if usage is None:
        return None
    return make_usage_sample(
        usage,
        cache_hit_tokens=read_usage_int(usage, "prompt_cache_hit_tokens"),
        cache_miss_tokens=read_usage_int(usage, "prompt_cache_miss_tokens"),
    )


class DeepSeekTierOptions(BaseModel):
    """DeepSeek-specific tier request options."""

    model_config = ConfigDict(extra="forbid")

    thinking: bool = True
    reasoning_effort: str = "high"
    extra_body: dict[str, Any] = Field(default_factory=dict)


def _default_tiers() -> dict[str, ResolvedTier[DeepSeekTierOptions]]:
    """Return built-in DeepSeek defaults for strong, cheap and fast tiers."""
    return {
        "strong": ResolvedTier(
            model="deepseek-v4-pro",
            options=DeepSeekTierOptions(),
        ),
        "cheap": ResolvedTier(
            model="deepseek-v4-flash",
            options=DeepSeekTierOptions(),
        ),
        "fast": ResolvedTier(
            model="deepseek-v4-flash",
            options=DeepSeekTierOptions(thinking=True),
        ),
    }


def build_request_kwargs(
    tier_config: ResolvedTier[DeepSeekTierOptions],
    messages: Messages,
    *,
    json_mode: bool = False,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    """Convert generic arguments into DeepSeek thinking-mode request parameters."""
    kwargs = base_request_kwargs(tier_config.model, messages, json_mode=json_mode)
    extra_body: dict[str, Any] = {
        "thinking": {"type": "enabled" if tier_config.options.thinking else "disabled"}
    }
    if tier_config.options.thinking:
        kwargs["reasoning_effort"] = tier_config.options.reasoning_effort
    if tier_config.options.extra_body:
        extra_body = deep_merge(extra_body, tier_config.options.extra_body)
    kwargs["extra_body"] = extra_body
    if max_tokens is not None:
        kwargs["max_tokens"] = max(max_tokens, 4096) if tier_config.options.thinking else max_tokens
    return kwargs


class DeepSeekClient(OpenAICompatibleBaseClient[DeepSeekTierOptions]):
    def __init__(self, cfg: LLMConfig):
        """Merge DeepSeek tier defaults and user overrides, then initialize the compatible
        client.
        """
        tiers = resolve_provider_tiers(
            cfg.tiers,
            options_type=DeepSeekTierOptions,
            defaults=_default_tiers(),
        )
        super().__init__(
            cfg,
            provider_name="DeepSeek",
            default_base_url=DEFAULT_BASE_URL,
            default_api_key_env=DEFAULT_API_KEY_ENV,
            tiers=tiers,
            requires_api_key=True,
        )

    def _normalize_usage(self, usage: Any) -> UsageSample | None:
        """Normalize DeepSeek's top-level cache fields into shared usage accounting."""
        return normalize_deepseek_usage(usage)

    def _build_request_kwargs(
        self,
        tier_config: ResolvedTier[DeepSeekTierOptions],
        messages: Messages,
        *,
        json_mode: bool,
        max_tokens: int | None,
    ) -> dict[str, Any]:
        """Build final request arguments for the selected DeepSeek tier."""
        return build_request_kwargs(
            tier_config,
            messages,
            json_mode=json_mode,
            max_tokens=max_tokens,
        )
