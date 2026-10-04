"""Arbitrary OpenAI Chat Completions-compatible endpoints and reasoning dialects."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..configuration import ProviderConfig
from ..profiles import get_profile
from ..transport import Messages, ResolvedModel
from ._credentials import configured_base_url
from ._openai_compatible import (
    OpenAICompatibleBaseClient,
    base_request_kwargs,
    deep_merge,
)
from ._options import ReasoningStyle, WireConnectionOptions


class CompatibleConnectionOptions(WireConnectionOptions):
    """Connection options for the SDK-based compatible providers."""


class OpenAICompatibleOptions(BaseModel):
    """Generic compatible-endpoint options; pass unknown fields through request_overrides."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    thinking: bool = False
    reasoning_effort: str = "high"
    json_response_fallback: Literal["none", "reasoning_content"] = "none"
    request_overrides: dict[str, Any] = Field(default_factory=dict)


def dialect_reasoning(
    reasoning_style: ReasoningStyle,
    *,
    thinking: bool,
    effort: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(top_level, extra_body)`` for one OpenAI-compatible reasoning dialect.

    ``none`` adds nothing; ``deepseek`` toggles ``thinking``; ``openai`` sets a top-level
    effort (``none`` when disabled); ``openrouter`` carries a ``reasoning`` object.
    """
    top_level: dict[str, Any] = {}
    extra_body: dict[str, Any] = {}
    if reasoning_style == "deepseek":
        extra_body["thinking"] = {"type": "enabled" if thinking else "disabled"}
        if thinking:
            top_level["reasoning_effort"] = effort
    elif reasoning_style == "openai":
        top_level["reasoning_effort"] = effort if thinking else "none"
    elif reasoning_style == "openrouter":
        extra_body["reasoning"] = {"effort": effort} if thinking else {"enabled": False}
    return top_level, extra_body


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
    reasoning_kwargs, extra_body = dialect_reasoning(
        reasoning_style,
        thinking=model_config.options.thinking,
        effort=model_config.options.reasoning_effort,
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

    def __init__(self, cfg: ProviderConfig):
        super().__init__(cfg)
        self.base_url = self.endpoint(cfg)

    @classmethod
    def endpoint(cls, cfg: ProviderConfig) -> str | None:
        """Resolve custom endpoint overrides consistently with the provider selector."""
        if cfg.kind == "openai-compatible":
            return configured_base_url(get_profile(cfg.kind), cfg.base_url)
        return cfg.base_url or cls.default_base_url

    @classmethod
    def validate_connection(cls, cfg: ProviderConfig) -> None:
        cls.connection_options.model_validate(cfg.model_extra or {})
        endpoint = cls.endpoint(cfg)
        if cls.requires_base_url and not endpoint:
            raise ValueError(f"Provider {cfg.kind} requires base_url")
        if endpoint:
            ProviderConfig.validate_url(endpoint)

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
