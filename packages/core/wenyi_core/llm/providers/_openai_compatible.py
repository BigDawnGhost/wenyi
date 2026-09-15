"""OpenAI-compatible wire protocol and single-attempt response handling."""

from __future__ import annotations

import json
import os
from abc import abstractmethod
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

from ..retrying import EmptyResponseError
from ..transport import Messages, ProviderAdapter, RequestContext, ResolvedModel
from ..usage import UsageSample, make_usage_sample, read_usage_int, read_usage_value

OptionsT = TypeVar("OptionsT", bound=BaseModel)
_JSON_MODE_INSTRUCTION = "Output must be valid json."


def base_request_kwargs(
    model: str,
    messages: Messages,
    *,
    json_mode: bool,
) -> dict[str, Any]:
    """Build base Chat Completions arguments and add explicit JSON instructions when requested."""
    request_messages = messages
    if json_mode:
        request_messages = [dict(message) for message in messages]
        for message in request_messages:
            if message.get("role") == "system":
                message["content"] = f"{message.get('content', '')}\n\n{_JSON_MODE_INSTRUCTION}"
                break
        else:
            request_messages.insert(
                0,
                {"role": "system", "content": _JSON_MODE_INSTRUCTION},
            )
        # Some gateways validate only user content, including when mapping to Responses input.
        # Mentioning JSON only in the system message may be insufficient,
        # so also append the instruction to the final user message.
        for message in reversed(request_messages):
            if message.get("role") == "user":
                content = str(message.get("content", ""))
                if "json" not in content.lower():
                    message["content"] = f"{content}\n\n{_JSON_MODE_INSTRUCTION}"
                break
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": request_messages,
        "stream": False,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    return kwargs


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge provider request bodies, preferring user values."""
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def normalize_openai_usage(usage: Any) -> UsageSample | None:
    """Normalize OpenAI-style nested cache details into shared usage accounting."""
    if usage is None:
        return None
    details = read_usage_value(usage, "prompt_tokens_details")
    cached_value = read_usage_value(details, "cached_tokens")
    if cached_value is None:
        cache_hit_tokens = 0
        cache_miss_tokens = 0
    else:
        cache_hit_tokens = read_usage_int(details, "cached_tokens")
        cache_miss_tokens = max(
            0,
            read_usage_int(usage, "prompt_tokens") - cache_hit_tokens,
        )
    return make_usage_sample(
        usage,
        cache_hit_tokens=cache_hit_tokens,
        cache_miss_tokens=cache_miss_tokens,
    )


class OpenAICompatibleBaseClient(ProviderAdapter, Generic[OptionsT]):
    """Reuse one SDK connection; route selection and usage belong to the caller."""

    def _ensure_client(self) -> Any:
        with self._client_lock:
            if self._client is None:
                from openai import OpenAI

                self.validate_credentials()
                api_key = os.environ.get(self.api_key_env) if self.api_key_env else None
                self._client = OpenAI(
                    api_key=api_key or "no-key",
                    base_url=self.base_url,
                    timeout=self.cfg.timeout,
                    max_retries=0,
                )
        return self._client

    @classmethod
    def output_limit(cls, options: BaseModel, hint: int | None, explicit: int | None) -> int | None:
        thinking = bool(getattr(options, "thinking", False))
        if explicit is not None:
            if thinking and explicit < 4096:
                raise ValueError(
                    "Thinking mode requires max_output_tokens >= 4096; disable thinking or increase the explicit limit"
                )
            return explicit
        return max(hint, 4096) if thinking and hint is not None else hint

    def _normalize_usage(self, usage: Any) -> UsageSample | None:
        return normalize_openai_usage(usage)

    def _json_response_fallback(
        self, model_config: ResolvedModel[OptionsT], message: Any
    ) -> str | None:
        return None

    @abstractmethod
    def _build_request_kwargs(
        self,
        model_config: ResolvedModel[OptionsT],
        messages: Messages,
        *,
        json_mode: bool,
        max_tokens: int | None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def _request(
        self,
        messages: Messages,
        model: ResolvedModel[OptionsT],
        *,
        json_mode: bool,
        context: RequestContext,
    ) -> str:
        model_config = model
        kwargs = self._build_request_kwargs(
            model, messages, json_mode=json_mode, max_tokens=context.max_tokens
        )
        response = self._ensure_client().chat.completions.create(**kwargs)
        context.record_usage(self._normalize_usage(getattr(response, "usage", None)))
        choice = response.choices[0]
        message = choice.message
        raw_content = getattr(message, "content", None)
        content = raw_content if isinstance(raw_content, str) else ""
        if not content.strip():
            if str(getattr(choice, "finish_reason", "")).lower() == "length":
                raise RuntimeError("OpenAI-compatible response was truncated at the token limit")
            fallback = self._json_response_fallback(model_config, message) if json_mode else None
            if fallback is None or not fallback.strip():
                raise EmptyResponseError(f"{self.cfg.kind} response content is empty")
            try:
                json.loads(fallback)
            except json.JSONDecodeError as error:
                raise EmptyResponseError(
                    f"{self.cfg.kind} configured JSON fallback response is invalid JSON"
                ) from error
            content = fallback
        return content
