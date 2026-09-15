"""Arbitrary OpenAI Chat Completions-compatible endpoints and reasoning dialects."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..transport import Messages, ResolvedModel
from ._openai_compatible import (
    OpenAICompatibleBaseClient,
    base_request_kwargs,
    deep_merge,
)

ReasoningStyle = Literal["none", "deepseek", "openai", "openrouter"]


class CompatibleConnectionOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    reasoning_style: ReasoningStyle = "none"


class OpenAICompatibleOptions(BaseModel):
    """Generic compatible-endpoint options; pass unknown fields through request_overrides."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    thinking: bool = False
    reasoning_effort: str = "high"
    json_response_fallback: Literal["none", "reasoning_content"] = "none"
    request_overrides: dict[str, Any] = Field(default_factory=dict)


def _reasoning_body(
    options: OpenAICompatibleOptions,
    reasoning_style: ReasoningStyle,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return SDK arguments and dialect fields that require raw request-body forwarding."""
    kwargs: dict[str, Any] = {}
    extra_body: dict[str, Any] = {}
    if reasoning_style == "deepseek":
        extra_body["thinking"] = {"type": "enabled" if options.thinking else "disabled"}
        if options.thinking:
            kwargs["reasoning_effort"] = options.reasoning_effort
    elif reasoning_style == "openai":
        kwargs["reasoning_effort"] = options.reasoning_effort if options.thinking else "none"
    elif reasoning_style == "openrouter":
        extra_body["reasoning"] = (
            {"effort": options.reasoning_effort} if options.thinking else {"enabled": False}
        )
    return kwargs, extra_body


def build_request_kwargs(
    model_config: ResolvedModel[OpenAICompatibleOptions],
    messages: Messages,
    *,
    json_mode: bool = False,
    max_tokens: int | None = None,
    reasoning_style: ReasoningStyle = "none",
) -> dict[str, Any]:
    """Build compatible request arguments according to the configured reasoning dialect."""
    kwargs = base_request_kwargs(model_config.model, messages, json_mode=json_mode)
    reasoning_kwargs, extra_body = _reasoning_body(
        model_config.options,
        reasoning_style,
    )
    kwargs.update(reasoning_kwargs)
    if model_config.options.request_overrides:
        extra_body = deep_merge(
            extra_body,
            model_config.options.request_overrides,
        )
    if extra_body:
        kwargs["extra_body"] = extra_body
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    return kwargs


class OpenAICompatibleClient(OpenAICompatibleBaseClient[OpenAICompatibleOptions]):
    connection_options = CompatibleConnectionOptions

    @property
    def reasoning_style(self) -> ReasoningStyle:
        return CompatibleConnectionOptions.model_validate(
            self.cfg.model_extra or {}
        ).reasoning_style

    def _json_response_fallback(
        self, model_config: ResolvedModel[OpenAICompatibleOptions], message: Any
    ) -> str | None:
        if model_config.options.json_response_fallback != "reasoning_content":
            return None
        value = getattr(message, "reasoning_content", None)
        return value if isinstance(value, str) else None

    def _build_request_kwargs(
        self,
        model_config: ResolvedModel[OpenAICompatibleOptions],
        messages: Messages,
        *,
        json_mode: bool,
        max_tokens: int | None,
    ) -> dict[str, Any]:
        return build_request_kwargs(
            model_config,
            messages,
            json_mode=json_mode,
            max_tokens=max_tokens,
            reasoning_style=self.reasoning_style,
        )
