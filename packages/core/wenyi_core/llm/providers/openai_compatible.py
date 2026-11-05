"""Arbitrary OpenAI Chat Completions-compatible endpoints and reasoning dialects."""

from __future__ import annotations

import json
from threading import Lock
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..configuration import ProviderConfig
from ..retrying import error_status_code
from ..transport import Messages, RequestContext, ResolvedModel
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
    json_output_mode: Literal["json_object", "json_schema"] = "json_object"
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
    if json_mode and model_config.options.json_output_mode == "json_schema":
        kwargs["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "response", "schema": {"type": "object"}, "strict": True},
        }
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
    protocol_version = 2

    def __init__(self, cfg: ProviderConfig, *, credentials: tuple[str | None] | None = None):
        super().__init__(cfg, credentials=credentials)
        # The adapter already scopes this cache to a frozen connection/credential snapshot.
        self._json_modes: dict[tuple[str, str], str] = {}
        self._json_modes_lock = Lock()

    def generate(
        self,
        messages: Messages,
        model: ResolvedModel[OpenAICompatibleOptions],
        *,
        json_mode: bool,
        context: RequestContext,
    ) -> str:
        if not json_mode or not context.allow_json_negotiation:
            return super().generate(messages, model, json_mode=json_mode, context=context)

        key = (model.model, json.dumps(model.options.model_dump(mode="json"), sort_keys=True))
        candidates = dict(self.json_probe_candidates(model))
        configured = model.options.json_output_mode
        with self._json_modes_lock:
            mode = self._json_modes.get(key, configured)
        source = "configured" if mode == configured else "cached"
        switched = False

        def attempt() -> str:
            # Never hold the cache lock while waiting for quotas, HTTP, or retry backoff.
            with context.attempt_scope():
                context.emit(
                    "llm_json_mode_selected", json_output_mode=mode, json_mode_source=source
                )
                return self._request(messages, candidates[mode], json_mode=True, context=context)

        def request() -> str:
            nonlocal mode, source, switched
            try:
                return attempt()
            except Exception as error:
                if switched or not self.is_json_mode_unsupported(error):
                    raise
                with self._json_modes_lock:
                    if self._json_modes.get(key) == mode:
                        del self._json_modes[key]
                # Retain this per-call choice across shared transient retries; switch only once.
                mode = next(candidate for candidate in candidates if candidate != mode)
                source = "fallback"
                switched = True
            return attempt()

        text = self._generate_with_retry(request, context=context)
        try:
            valid_object = isinstance(json.loads(text), dict)
        except ValueError:
            valid_object = False
        if valid_object:
            with self._json_modes_lock:
                self._json_modes[key] = mode
        # Leave task-level parsing/validation unchanged; invalid output is never cached.
        return text

    def json_probe_candidates(
        self, model: ResolvedModel[OpenAICompatibleOptions]
    ) -> tuple[tuple[str, ResolvedModel], ...]:
        selected = model.options.json_output_mode
        alternate = "json_schema" if selected == "json_object" else "json_object"
        return tuple(
            (
                mode,
                ResolvedModel(
                    model.model, model.options.model_copy(update={"json_output_mode": mode})
                ),
            )
            for mode in (selected, alternate)
        )

    def is_json_mode_unsupported(self, error: Exception) -> bool:
        if error_status_code(error) not in {400, 422}:
            return False
        # Inspect locally, before safe error projection; never retain provider error text.
        body = getattr(error, "body", None)
        detail = body.get("error", body) if isinstance(body, dict) else body
        if isinstance(detail, dict):
            detail = detail.get("message")
        if not isinstance(detail, str):
            return False
        text = detail.lower()
        return (
            "response_format" in text
            and any(mode in text for mode in ("json_object", "json_schema"))
            and any(word in text for word in ("must", "only", "unsupported", "not supported"))
        )

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
