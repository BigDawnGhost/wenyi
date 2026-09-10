"""Call DeepSeek through its native OpenAI-compatible endpoint."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..transport import Messages, ResolvedModel
from ..usage import UsageSample, make_usage_sample, read_usage_int
from ._openai_compatible import (
    OpenAICompatibleBaseClient,
    base_request_kwargs,
    deep_merge,
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


class DeepSeekOptions(BaseModel):
    """DeepSeek-specific model request options."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    thinking: bool = True
    reasoning_effort: str = "high"
    extra_body: dict[str, Any] = Field(default_factory=dict)


def preset_models() -> dict[str, ResolvedModel[DeepSeekOptions]]:
    """Return built-in DeepSeek defaults for strong, cheap and fast tiers."""
    return {
        "strong": ResolvedModel(
            model="deepseek-v4-flash",
            options=DeepSeekOptions(),
        ),
        "cheap": ResolvedModel(
            model="deepseek-v4-flash",
            options=DeepSeekOptions(),
        ),
        "fast": ResolvedModel(
            model="deepseek-v4-flash",
            options=DeepSeekOptions(),
        ),
    }


def build_request_kwargs(
    model_config: ResolvedModel[DeepSeekOptions],
    messages: Messages,
    *,
    json_mode: bool = False,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    """Convert generic arguments into DeepSeek thinking-mode request parameters."""
    kwargs = base_request_kwargs(model_config.model, messages, json_mode=json_mode)
    extra_body: dict[str, Any] = {
        "thinking": {"type": "enabled" if model_config.options.thinking else "disabled"}
    }
    if model_config.options.thinking:
        kwargs["reasoning_effort"] = model_config.options.reasoning_effort
    if model_config.options.extra_body:
        extra_body = deep_merge(extra_body, model_config.options.extra_body)
    kwargs["extra_body"] = extra_body
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    return kwargs


class DeepSeekClient(OpenAICompatibleBaseClient[DeepSeekOptions]):
    default_base_url = DEFAULT_BASE_URL
    default_api_key_env = DEFAULT_API_KEY_ENV
    requires_api_key = True

    def _normalize_usage(self, usage: Any) -> UsageSample | None:
        """Normalize DeepSeek's top-level cache fields into shared usage accounting."""
        return normalize_deepseek_usage(usage)

    def _build_request_kwargs(
        self,
        model_config: ResolvedModel[DeepSeekOptions],
        messages: Messages,
        *,
        json_mode: bool,
        max_tokens: int | None,
    ) -> dict[str, Any]:
        """Build final request arguments for the selected DeepSeek tier."""
        return build_request_kwargs(
            model_config,
            messages,
            json_mode=json_mode,
            max_tokens=max_tokens,
        )
