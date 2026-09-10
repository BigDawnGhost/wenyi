"""Google Gemini provider using the official google-genai SDK."""

from __future__ import annotations

import os
from math import ceil
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..retrying import EmptyResponseError
from ..transport import Messages, ProviderAdapter, RequestContext, ResolvedModel
from ..usage import UsageSample, make_usage_sample, read_usage_int

DEFAULT_API_KEY_ENV = "GEMINI_API_KEY"
FALLBACK_API_KEY_ENV = "GOOGLE_API_KEY"


class GeminiOptions(BaseModel):
    """Gemini-specific model request options."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    thinking_level: str | None = None
    thinking_budget: int | None = None
    temperature: float | None = None
    extra_body: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_thinking_options(self) -> GeminiOptions:
        """Require thinking_level and thinking_budget to be mutually exclusive."""
        if self.thinking_level is not None and self.thinking_budget is not None:
            raise ValueError("thinking_level and thinking_budget are mutually exclusive")
        return self


def preset_models() -> dict[str, ResolvedModel[GeminiOptions]]:
    """Return built-in Gemini defaults for strong, cheap and fast tiers."""
    return {
        "strong": ResolvedModel(
            model="gemini-3.6-flash",
            options=GeminiOptions(),
        ),
        "cheap": ResolvedModel(
            model="gemini-3.6-flash",
            options=GeminiOptions(),
        ),
        "fast": ResolvedModel(
            model="gemini-3.6-flash",
            options=GeminiOptions(),
        ),
    }


def convert_messages_to_gemini(
    messages: Messages,
) -> tuple[str | None, list[dict[str, Any]]]:
    """Convert OpenAI-style messages into Gemini system_instruction and contents.
    Merge system messages into system_instruction, keep user roles and convert assistant
    roles to model.
    """
    system_parts: list[str] = []
    contents: list[dict[str, Any]] = []

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "") or ""

        if role == "system":
            if content.strip():
                system_parts.append(content)
        elif role == "assistant":
            contents.append(
                {
                    "role": "model",
                    "parts": [{"text": content}],
                }
            )
        else:  # user or others
            contents.append(
                {
                    "role": "user",
                    "parts": [{"text": content}],
                }
            )

    system_instruction = "\n\n".join(system_parts) if system_parts else None
    return system_instruction, contents


def extract_gemini_usage(usage_metadata: Any) -> UsageSample | None:
    """Normalize Gemini UsageMetadata, including prompt/cached-content tokens,
    candidate/thought tokens and total tokens.
    """
    if usage_metadata is None:
        return None

    prompt_tokens = read_usage_int(usage_metadata, "prompt_token_count")
    completion_tokens = read_usage_int(usage_metadata, "candidates_token_count") + read_usage_int(
        usage_metadata, "thoughts_token_count"
    )
    total_tokens = read_usage_int(usage_metadata, "total_token_count") or (
        prompt_tokens + completion_tokens
    )

    cache_hit_tokens = read_usage_int(usage_metadata, "cached_content_token_count")
    cache_miss_tokens = max(0, prompt_tokens - cache_hit_tokens)

    return make_usage_sample(
        {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        },
        cache_hit_tokens=cache_hit_tokens,
        cache_miss_tokens=cache_miss_tokens,
    )


def get_api_key_from_env(custom_env: str | None = None) -> tuple[str | None, str]:
    """Resolve the Gemini API key from custom_env first, then GEMINI_API_KEY, then
    GOOGLE_API_KEY.
    """
    if custom_env:
        val = os.environ.get(custom_env, "").strip()
        if val:
            return val, custom_env

    val_gemini = os.environ.get(DEFAULT_API_KEY_ENV, "").strip()
    if val_gemini:
        return val_gemini, DEFAULT_API_KEY_ENV

    val_google = os.environ.get(FALLBACK_API_KEY_ENV, "").strip()
    if val_google:
        return val_google, FALLBACK_API_KEY_ENV

    target_env = custom_env or DEFAULT_API_KEY_ENV
    return None, target_env


class GeminiClient(ProviderAdapter):
    """Wrapper for the official Google Gemini SDK client."""

    default_api_key_env = DEFAULT_API_KEY_ENV
    default_base_url = "https://generativelanguage.googleapis.com"
    requires_api_key = True

    def validate_credentials(self) -> None:
        """Validate Gemini API-key configuration."""
        api_key, target_env = get_api_key_from_env(self.cfg.api_key_env)
        if not api_key:
            raise RuntimeError(
                f"Environment variable {target_env} (or {FALLBACK_API_KEY_ENV}) is not set"
            )

    def _ensure_client(self) -> Any:
        """Create and validate google.genai.Client lazily."""
        with self._client_lock:
            if self._client is None:
                try:
                    from google import genai
                except ImportError as error:
                    raise RuntimeError(
                        "The google-genai SDK is required: pip install google-genai"
                        " (or run uv add google-genai)"
                    ) from error

                self.validate_credentials()
                api_key, _ = get_api_key_from_env(self.cfg.api_key_env)

                kwargs: dict[str, Any] = {
                    "api_key": api_key,
                    # ProviderConfig.timeout is expressed in seconds; google-genai HttpOptions
                    # expects milliseconds.
                    "http_options": {
                        "timeout": ceil(self.cfg.timeout * 1000),
                        "retry_options": {"attempts": 1},
                    },
                }
                if self.cfg.base_url:
                    kwargs["http_options"].update({"base_url": self.cfg.base_url})

                self._client = genai.Client(**kwargs)
        return self._client

    def _request(
        self,
        messages: Messages,
        model: ResolvedModel[GeminiOptions],
        *,
        json_mode: bool,
        context: RequestContext,
    ) -> str:
        model_config = model
        client = self._ensure_client()
        system_instruction, contents = convert_messages_to_gemini(messages)

        # Build GenerateContentConfig.
        config_kwargs: dict[str, Any] = {}
        if system_instruction:
            config_kwargs["system_instruction"] = system_instruction

        if json_mode:
            config_kwargs["response_mime_type"] = "application/json"

        # Apply the output-token limit.
        effective_max_tokens = context.max_tokens
        if effective_max_tokens is not None:
            config_kwargs["max_output_tokens"] = effective_max_tokens

        if model_config.options.temperature is not None:
            config_kwargs["temperature"] = model_config.options.temperature

        # Apply thinking options.
        if (
            model_config.options.thinking_level is not None
            or model_config.options.thinking_budget is not None
        ):
            try:
                from google.genai import types

                thinking_kwargs: dict[str, Any] = {}
                if model_config.options.thinking_level is not None:
                    thinking_kwargs["thinking_level"] = model_config.options.thinking_level
                if model_config.options.thinking_budget is not None:
                    thinking_kwargs["thinking_budget"] = model_config.options.thinking_budget
                config_kwargs["thinking_config"] = types.ThinkingConfig(**thinking_kwargs)
            except (ImportError, AttributeError):  # pragma: no cover
                pass

        if model_config.options.extra_body:
            config_kwargs.update(model_config.options.extra_body)

        response = client.models.generate_content(
            model=model_config.model,
            contents=contents,
            config=config_kwargs,
        )

        # Record usage.
        sample = extract_gemini_usage(getattr(response, "usage_metadata", None))
        context.record_usage(sample)

        # Validate the response and check safety blocking.
        candidates = getattr(response, "candidates", None)
        if not candidates:
            raise RuntimeError("Gemini API returned no candidates")

        candidate = candidates[0]
        finish_reason = str(getattr(candidate, "finish_reason", ""))
        if "SAFETY" in finish_reason.upper() or "BLOCK" in finish_reason.upper():
            raise RuntimeError(f"Gemini API blocked the response (finish_reason={finish_reason})")

        text = getattr(response, "text", None)
        if not isinstance(text, str):
            # Try reading text from parts.
            content = getattr(candidate, "content", None)
            parts = getattr(content, "parts", []) if content else []
            parts_text = [
                str(getattr(p, "text", "")) for p in parts if getattr(p, "text", None) is not None
            ]
            text = "".join(parts_text) if parts_text else ""

        if not text or not text.strip():
            raise EmptyResponseError("Gemini response content is empty")
        return text
