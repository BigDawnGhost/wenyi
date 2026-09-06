"""Shared transport, retries and tier resolution for OpenAI-compatible providers."""

from __future__ import annotations

import json
import os
import threading
from abc import abstractmethod
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

from ...config import LLMConfig, TierConfig
from ..base import LLMClient, Messages
from ..retrying import EmptyResponseError, RetryReporter, provider_retry
from ..tiers import resolve_tier
from ..usage import (
    UsageSample,
    make_usage_sample,
    read_usage_int,
    read_usage_value,
)

OptionsT = TypeVar("OptionsT", bound=BaseModel)
_JSON_MODE_INSTRUCTION = "Output must be valid json."


@dataclass(frozen=True)
class ResolvedTier(Generic[OptionsT]):
    """A runtime tier completed and validated by its provider."""

    model: str
    options: OptionsT


def resolve_provider_tiers(
    overrides: dict[str, TierConfig],
    *,
    options_type: type[OptionsT],
    defaults: dict[str, ResolvedTier[OptionsT]] | None = None,
) -> dict[str, ResolvedTier[OptionsT]]:
    """Merge common tier overrides and validate through the provider-specific options model."""
    tiers = dict(defaults or {})
    for name, override in overrides.items():
        current = tiers.get(name)
        model = override.model or (current.model if current else None)
        if not model:
            raise ValueError(f"llm.tiers.{name}.model must not be empty")
        option_values = current.options.model_dump() if current else {}
        option_values.update(override.options)
        tiers[name] = ResolvedTier(
            model=model,
            options=options_type.model_validate(option_values),
        )
    if "strong" not in tiers:
        raise ValueError("Configuration is missing llm.tiers.strong.model")
    return tiers


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


class OpenAICompatibleBaseClient(LLMClient, Generic[OptionsT]):
    """Shared client for OpenAI Chat Completions-compatible providers."""

    def __init__(
        self,
        cfg: LLMConfig,
        *,
        provider_name: str,
        default_base_url: str | None,
        default_api_key_env: str | None,
        tiers: dict[str, ResolvedTier[OptionsT]],
        requires_api_key: bool,
    ) -> None:
        """Resolve connection settings and validated tiers; create the SDK client lazily."""
        super().__init__()
        self.cfg = cfg
        self.provider_name = provider_name
        self.base_url = cfg.base_url or default_base_url
        self.api_key_env = cfg.api_key_env or default_api_key_env
        self.tiers = tiers
        self.requires_api_key = requires_api_key
        if not self.base_url:
            raise ValueError(f"{provider_name} requires llm.base_url")
        self._client: Any = None
        self._client_lock = threading.Lock()

    def _ensure_client(self) -> Any:
        """Create the OpenAI SDK client lazily under a lock and validate its API key."""
        with self._client_lock:
            if self._client is None:
                try:
                    from openai import OpenAI
                except ImportError as error:  # pragma: no cover
                    raise RuntimeError(
                        "The openai SDK is required: pip install openai"
                        " (or set llm.provider to fake for offline testing)"
                    ) from error
                self.validate_credentials()
                api_key = os.environ.get(self.api_key_env) if self.api_key_env else None
                self._client = OpenAI(
                    api_key=api_key or "no-key",
                    base_url=self.base_url,
                    timeout=self.cfg.timeout,
                    # Wenyi owns retry classification, backoff and events; disable nested SDK retries.
                    max_retries=0,
                )
        return self._client

    def validate_credentials(self) -> None:
        """Report a missing API-key environment variable before starting model workflows."""
        if not self.api_key_env:
            if self.requires_api_key:
                raise RuntimeError(f"{self.provider_name} requires llm.api_key_env")
            return
        api_key = os.environ.get(self.api_key_env, "").strip()
        if (self.requires_api_key or self.api_key_env) and not api_key:
            raise RuntimeError(
                f"Environment variable {self.api_key_env} ({self.provider_name} API key) is not set"
            )

    def _normalize_usage(self, usage: Any) -> UsageSample | None:
        """Read standard OpenAI-compatible cache usage from nested details."""
        return normalize_openai_usage(usage)

    def _json_response_fallback(
        self,
        tier_config: ResolvedTier[OptionsT],
        message: Any,
    ) -> str | None:
        """Return explicitly enabled JSON fallback fields; distrust nonstandard fields by
        default.
        """
        return None

    @abstractmethod
    def _build_request_kwargs(
        self,
        tier_config: ResolvedTier[OptionsT],
        messages: Messages,
        *,
        json_mode: bool,
        max_tokens: int | None,
    ) -> dict[str, Any]:
        """Convert a generic call to the provider's request dialect."""
        raise NotImplementedError

    def complete(
        self,
        messages: Messages,
        *,
        tier: str = "strong",
        json_mode: bool = False,
        max_tokens: int | None = None,
        stage: str | None = None,
    ) -> str:
        """Call the compatible endpoint at the requested tier with retries and normalized usage
        accounting.
        """
        tier_config = resolve_tier(self.tiers, tier)
        kwargs = self._build_request_kwargs(
            tier_config,
            messages,
            json_mode=json_mode,
            max_tokens=max_tokens,
        )
        client = self._ensure_client()

        reporter = RetryReporter(
            provider=self.provider_name,
            tier=tier,
            stage=stage,
            max_attempts=max(1, self.cfg.max_retries + 1),
            emit=self._emit_event,
        )

        @provider_retry(self.cfg.max_retries, reporter)
        def _call() -> str:
            """Perform one request; let the tenacity retry decorator handle exceptions."""
            response = client.chat.completions.create(**kwargs)
            sample = self._normalize_usage(getattr(response, "usage", None))
            self.usage.record(tier, sample, stage)
            choice = response.choices[0]
            message = choice.message
            raw_content = getattr(message, "content", None)
            content = raw_content if isinstance(raw_content, str) else ""
            if not content.strip():
                if str(getattr(choice, "finish_reason", "")).lower() == "length":
                    raise RuntimeError(
                        "OpenAI-compatible response was truncated at the token limit"
                    )
                fallback = self._json_response_fallback(tier_config, message) if json_mode else None
                if fallback is None or not fallback.strip():
                    raise EmptyResponseError(f"{self.provider_name} response content is empty")
                try:
                    json.loads(fallback)
                except json.JSONDecodeError as error:
                    raise EmptyResponseError(
                        f"{self.provider_name} configured JSON fallback response is invalid JSON"
                    ) from error
                content = fallback
            return content

        return _call()
