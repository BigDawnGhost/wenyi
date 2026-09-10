"""Call models through OpenRouter's OpenAI-compatible endpoint."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..transport import Messages, ResolvedModel
from ._openai_compatible import (
    OpenAICompatibleBaseClient,
    base_request_kwargs,
    deep_merge,
)

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_API_KEY_ENV = "OPENROUTER_API_KEY"


class OpenRouterOptions(BaseModel):
    """OpenRouter-specific model request options."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    thinking: bool = True
    reasoning_effort: str = "high"
    extra_body: dict[str, Any] = Field(default_factory=dict)


def build_request_kwargs(
    model_config: ResolvedModel[OpenRouterOptions],
    messages: Messages,
    *,
    json_mode: bool = False,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    """Convert tier options into an OpenRouter reasoning request body."""
    kwargs = base_request_kwargs(model_config.model, messages, json_mode=json_mode)
    extra_body = {
        "reasoning": (
            {"effort": model_config.options.reasoning_effort}
            if model_config.options.thinking
            else {"enabled": False}
        )
    }
    if model_config.options.extra_body:
        extra_body = deep_merge(extra_body, model_config.options.extra_body)
    kwargs["extra_body"] = extra_body
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    return kwargs


class OpenRouterClient(OpenAICompatibleBaseClient[OpenRouterOptions]):
    default_base_url = DEFAULT_BASE_URL
    default_api_key_env = DEFAULT_API_KEY_ENV
    requires_api_key = True

    def _build_request_kwargs(
        self,
        model_config: ResolvedModel[OpenRouterOptions],
        messages: Messages,
        *,
        json_mode: bool,
        max_tokens: int | None,
    ) -> dict[str, Any]:
        """Build final request arguments for the selected OpenRouter tier."""
        return build_request_kwargs(
            model_config,
            messages,
            json_mode=json_mode,
            max_tokens=max_tokens,
        )
