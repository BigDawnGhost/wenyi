"""Provider transport implementations.

Model selection and thinking strategy are parsed into ModelRef by AgentRouter;
the transport builds requests, executes fixed retries, and records usage.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
import warnings
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from trans_novel.config import LLMConfig, ModelRef, ProviderRouting
from trans_novel.llm.base import Messages
from trans_novel.llm.generation import GenerationOptions
from trans_novel.llm.retrying import (
    MODEL_NOT_FOUND,
    EmptyResponseError,
    classify_fallback,
    classify_retry,
    status_code,
)
from trans_novel.llm.telemetry import CallAttemptTelemetry, CallTelemetrySink
from trans_novel.llm.usage import UsageTracker, has_response_usage, normalize_response_usage
from trans_novel.model_profiles import (
    DIALECT_BAILIAN,
    DIALECT_DEEPSEEK,
    DIALECT_OPENAI,
    DIALECT_OPENROUTER,
    ModelCapabilities,
)
from trans_novel.model_profiles import capabilities_for as _capabilities_for

_REASONING_FLOOR_TOKENS = 4096
_REQUEST_TIMEOUT_SECONDS = 600
_MAX_RETRIES = 4


def validate_generation_options(
    capabilities: ModelCapabilities,
    model_ref: ModelRef,
    options: GenerationOptions | None,
) -> None:
    """Reject controlled requests that the selected model cannot satisfy."""
    if options is None:
        return
    if options.require_catalogued_model and not capabilities.catalogued:
        raise ValueError(f"model is not catalogued: {model_ref.provider}:{model_ref.model}")
    if options.require_thinking_disabled:
        if model_ref.reasoning_enabled:
            raise ValueError("generation requires thinking to be disabled")
        if not capabilities.supports_thinking_disabled:
            raise ValueError(
                f"model does not support verified thinking disable: "
                f"{model_ref.provider}:{model_ref.model}"
            )
    if options.temperature is not None and not capabilities.supports_temperature:
        raise ValueError(
            f"model does not support verified temperature: {model_ref.provider}:{model_ref.model}"
        )
    if options.seed is not None and not capabilities.supports_seed:
        raise ValueError(
            f"model does not support verified seed: {model_ref.provider}:{model_ref.model}"
        )


@runtime_checkable
class ProviderTransport(Protocol):
    provider: str
    usage: UsageTracker

    def complete(
        self,
        messages: Messages,
        model_ref: ModelRef,
        *,
        json_mode: bool = False,
        max_tokens: int | None = None,
        stage: str | None = None,
        agent: str,
        operation: str,
        logical_call_id: str | None = None,
        attempt_counter: list[int] | None = None,
    ) -> str: ...


def build_request_kwargs(
    capabilities: ModelCapabilities,
    model_ref: ModelRef,
    messages: Messages,
    *,
    json_mode: bool = False,
    max_tokens: int | None = None,
    generation_options: GenerationOptions | None = None,
    provider_routing: ProviderRouting | None = None,
) -> dict[str, Any]:
    """按逐模型能力构造请求；路由意图只在模型明确支持时下发。"""
    validate_generation_options(capabilities, model_ref, generation_options)
    kwargs: dict[str, Any] = {
        "model": model_ref.model,
        "messages": messages,
        "stream": False,
    }
    reasoning_enabled = (
        model_ref.reasoning_enabled and model_ref.reasoning_effort in capabilities.reasoning_efforts
    )
    dialect = capabilities.request_dialect
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    if dialect == DIALECT_DEEPSEEK:
        if reasoning_enabled:
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
            kwargs["reasoning_effort"] = model_ref.reasoning_effort
        else:
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
    elif dialect == DIALECT_BAILIAN:
        kwargs["extra_body"] = {"enable_thinking": model_ref.reasoning_enabled}
        if (
            model_ref.reasoning_enabled
            and model_ref.reasoning_effort in capabilities.reasoning_efforts
        ):
            kwargs["reasoning_effort"] = model_ref.reasoning_effort
    elif dialect == DIALECT_OPENAI:
        if reasoning_enabled:
            kwargs["reasoning_effort"] = model_ref.reasoning_effort
    elif dialect == DIALECT_OPENROUTER:
        extra_body: dict[str, Any] = {
            "reasoning": (
                {"effort": model_ref.reasoning_effort} if reasoning_enabled else {"enabled": False}
            )
        }
        if provider_routing is not None:
            extra_body["provider"] = provider_routing.model_dump(exclude_none=True)
        kwargs["extra_body"] = extra_body
    if max_tokens is not None:
        kwargs["max_tokens"] = (
            max(max_tokens, _REASONING_FLOOR_TOKENS) if reasoning_enabled else max_tokens
        )
    if generation_options is not None:
        if generation_options.temperature is not None:
            kwargs["temperature"] = generation_options.temperature
        if generation_options.seed is not None:
            kwargs["seed"] = generation_options.seed
    return kwargs


def build_responses_request_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Convert validated chat-completions arguments for a Responses API model."""
    value = dict(kwargs)
    value["input"] = value.pop("messages")
    max_tokens = value.pop("max_tokens", None)
    if max_tokens is not None:
        value["max_output_tokens"] = max(16, max_tokens)
    response_format = value.pop("response_format", None)
    if response_format is not None:
        value["text"] = {"format": response_format}
    reasoning_effort = value.pop("reasoning_effort", None)
    if reasoning_effort is not None:
        value["reasoning"] = {"effort": reasoning_effort}
    return value


def _safe_attr(obj: Any, name: str) -> object:
    try:
        return getattr(obj, name, None)
    except Exception:
        return None


def _safe_text(value: object) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def warn_telemetry_failure() -> None:
    with suppress(Exception):
        warnings.warn("LLM telemetry write failed", RuntimeWarning, stacklevel=2)


def _started_at() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _telemetry_call_id(current: str | None, sink: CallTelemetrySink | None) -> str | None:
    return current or uuid.uuid4().hex if sink is not None else None


def _request_hash(kwargs: dict[str, Any]) -> str:
    payload = json.dumps(kwargs, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(payload).hexdigest()


def _response_meta(response: Any) -> tuple[str | None, str | None, str | None]:
    resolved_model = _safe_text(_safe_attr(response, "model"))
    response_id = _safe_text(_safe_attr(response, "id"))
    try:
        choice = response.choices[0]
    except Exception:
        return resolved_model, None, response_id
    return resolved_model, _safe_text(_safe_attr(choice, "finish_reason")), response_id


class OpenAICompatibleTransport:
    """Lazily create a client and use the fixed timeout/retry strategy."""

    def __init__(
        self,
        cfg: LLMConfig,
        usage: UsageTracker,
        *,
        provider: str,
        provider_name: str,
        default_base_url: str | None,
        default_api_key_env: str | None,
        requires_api_key: bool,
        generation_options: GenerationOptions | None = None,
        telemetry_sink: CallTelemetrySink | None = None,
    ) -> None:
        self.provider = provider
        self.cfg = cfg
        self.usage = usage
        self.provider_name = provider_name
        self.base_url = cfg.base_url if provider == "openai-compatible" else default_base_url
        self.api_key_env = (
            cfg.api_key_env if provider == "openai-compatible" else default_api_key_env
        )
        self.requires_api_key = requires_api_key
        if not self.base_url:
            raise ValueError(f"llm.base_url：{provider_name} provider 必须配置服务地址")
        self._client: Any = None
        self.generation_options = generation_options
        self.telemetry_sink = telemetry_sink
        self._default_headers = {"User-Agent": "wenyi"}
        if provider == "opencode-go":
            self._default_headers["x-opencode-session"] = str(uuid.uuid4())
        self._client_lock = threading.Lock()

    def capabilities_for(self, model: str) -> ModelCapabilities:
        return _capabilities_for(self.provider, model)

    def _ensure_client(self) -> Any:
        with self._client_lock:
            if self._client is None:
                try:
                    from openai import OpenAI
                except ImportError as error:  # pragma: no cover
                    raise RuntimeError("需要 openai SDK：pip install openai") from error
                api_key = os.environ.get(self.api_key_env) if self.api_key_env else None
                if (self.requires_api_key or self.api_key_env) and not api_key:
                    raise RuntimeError(
                        f"未设置环境变量 {self.api_key_env}（{self.provider_name} API key）"
                    )
                self._client = OpenAI(
                    api_key=api_key or "no-key",
                    base_url=self.base_url,
                    timeout=_REQUEST_TIMEOUT_SECONDS,
                    max_retries=0,
                    default_headers=self._default_headers,
                )
        return self._client

    def complete(
        self,
        messages: Messages,
        model_ref: ModelRef,
        *,
        json_mode: bool = False,
        max_tokens: int | None = None,
        stage: str | None = None,
        agent: str,
        operation: str,
        logical_call_id: str | None = None,
        attempt_counter: list[int] | None = None,
    ) -> str:
        capabilities = self.capabilities_for(model_ref.model)
        kwargs = build_request_kwargs(
            capabilities,
            model_ref,
            messages,
            json_mode=json_mode,
            max_tokens=max_tokens,
            generation_options=self.generation_options,
            provider_routing=self.cfg.provider_routing.get(f"{self.provider}/{model_ref.model}"),
        )
        if capabilities.responses_api:
            kwargs = build_responses_request_kwargs(kwargs)
        logical_call_id = _telemetry_call_id(logical_call_id, self.telemetry_sink)
        if self.telemetry_sink is not None:
            try:
                request_digest = _request_hash(kwargs)
            except Exception:
                request_digest = None
        else:
            request_digest = None
        attempt_index = 0

        def emit(
            *,
            started_at: str,
            elapsed_ms: int,
            status: str,
            retry_class: str | None,
            response: Any = None,
            content: str | None = None,
            usage: Any = None,
            http_status: int | None = None,
        ) -> None:
            if self.telemetry_sink is None or request_digest is None:
                return
            try:
                resolved_model = finish_reason = response_id = None
                if response is not None:
                    resolved_model, finish_reason, response_id = _response_meta(response)
                normalized = normalize_response_usage(usage)
                response_digest = (
                    hashlib.sha256(content.encode("utf-8")).hexdigest()
                    if isinstance(content, str)
                    else None
                )
                self.telemetry_sink.record(
                    CallAttemptTelemetry(
                        schema_version=1,
                        logical_call_id=logical_call_id,
                        attempt_index=attempt_index,
                        started_at=started_at,
                        elapsed_ms=elapsed_ms,
                        stage=stage,
                        agent=agent,
                        operation=operation,
                        provider=self.provider,
                        requested_model=model_ref.model,
                        resolved_model=resolved_model,
                        reasoning_enabled=model_ref.reasoning_enabled,
                        reasoning_effort=model_ref.reasoning_effort,
                        temperature=(
                            float(self.generation_options.temperature)
                            if self.generation_options is not None
                            and self.generation_options.temperature is not None
                            else None
                        ),
                        seed=self.generation_options.seed
                        if self.generation_options is not None
                        else None,
                        json_mode=json_mode,
                        max_tokens=max_tokens,
                        status=status,
                        retry_class=retry_class,
                        http_status=http_status,
                        finish_reason=finish_reason,
                        response_id=response_id,
                        **normalized,
                        billed_usage_unknown=not has_response_usage(usage),
                        request_sha256=request_digest,
                        response_sha256=response_digest,
                    )
                )
            except Exception:
                warn_telemetry_failure()

        def finish_attempt(usage: Any = None, *, failed: bool = False) -> None:
            self.usage.record_attempt_result(
                agent=agent,
                operation=operation,
                provider=self.provider,
                model_ref=model_ref,
                usage=usage,
                stage=stage,
                failed=failed,
            )

        client = self._ensure_client()

        @retry(
            stop=stop_after_attempt(_MAX_RETRIES + 1),
            wait=wait_exponential(multiplier=1, max=30),
            retry=retry_if_exception(
                lambda error: (
                    classify_fallback(error) != MODEL_NOT_FOUND
                    and classify_retry(error) is not None
                )
            ),
            reraise=True,
        )
        def call() -> str:
            nonlocal attempt_index
            if attempt_counter is None:
                attempt_index += 1
            else:
                attempt_counter[0] += 1
                attempt_index = attempt_counter[0]
            started_at = _started_at()
            started = time.monotonic()
            self.usage.begin_attempt()
            try:
                response = (
                    client.responses.create(**kwargs)
                    if capabilities.responses_api
                    else client.chat.completions.create(**kwargs)
                )
            except Exception as error:
                finish_attempt(failed=True)
                emit(
                    started_at=started_at,
                    elapsed_ms=round((time.monotonic() - started) * 1000),
                    status="error",
                    retry_class=classify_retry(error),
                    http_status=status_code(error),
                )
                raise
            try:
                usage = getattr(response, "usage", None)
            except Exception:
                finish_attempt(failed=True)
                emit(
                    started_at=started_at,
                    elapsed_ms=round((time.monotonic() - started) * 1000),
                    status="error",
                    retry_class=None,
                    response=response,
                )
                raise
            try:
                if capabilities.responses_api:
                    content = getattr(response, "output_text", None)
                else:
                    choices = getattr(response, "choices", None)
                    message = getattr(choices[0], "message", None) if choices else None
                    content = getattr(message, "content", None)
            except Exception:
                finish_attempt(usage, failed=True)
                emit(
                    started_at=started_at,
                    elapsed_ms=round((time.monotonic() - started) * 1000),
                    status="error",
                    retry_class=None,
                    response=response,
                    usage=usage,
                )
                raise
            empty = not isinstance(content, str) or not content.strip()
            finish_attempt(usage, failed=empty)
            if empty:
                emit(
                    started_at=started_at,
                    elapsed_ms=round((time.monotonic() - started) * 1000),
                    status="empty_response",
                    retry_class="empty_response",
                    response=response,
                    content=content,
                    usage=usage,
                )
                raise EmptyResponseError("provider returned empty response content")
            emit(
                started_at=started_at,
                elapsed_ms=round((time.monotonic() - started) * 1000),
                status="success",
                retry_class=None,
                response=response,
                content=content,
                usage=usage,
            )
            return content

        return call()
