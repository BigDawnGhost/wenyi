"""Provider transport contracts without workflow orchestration or tier selection."""

from __future__ import annotations

import os
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict

from .configuration import ProviderConfig
from .retrying import RetryReporter, provider_retry
from .usage import UsageSample

OptionsT = TypeVar("OptionsT", bound=BaseModel)
Messages = list[dict[str, str]]


@dataclass(frozen=True)
class ResolvedModel(Generic[OptionsT]):
    model: str
    options: OptionsT


class ConnectionOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


@dataclass
class RequestContext:
    """Per-call hooks; shared adapters never retain mutable request context."""

    operation: str
    tier: str
    max_tokens: int | None
    emit: Callable[..., None]
    record_usage: Callable[[UsageSample | None], None]
    attempt_scope: Callable[..., Any]
    sleep: Callable[[float], None] | None = None
    redact_diagnostic: Callable[[str], str] | None = None
    allow_json_negotiation: bool = True


class ProviderAdapter(ABC):
    """Own an SDK connection; the shared retry helper wraps each guarded transport attempt."""

    default_base_url: str | None = None
    default_api_key_env: str | None = None
    requires_api_key = False
    requires_base_url = True
    protocol_version = 1
    connection_options: type[BaseModel] = ConnectionOptions

    def __init__(self, cfg: ProviderConfig, *, credentials: tuple[str | None] | None = None):
        self.validate_connection(cfg)
        self.cfg = cfg
        self.base_url = cfg.base_url or self.default_base_url
        self.api_key_env = cfg.api_key_env or self.default_api_key_env
        self._credentials = credentials
        self._client: Any = None
        self._client_lock = threading.Lock()

    @classmethod
    def validate_connection(cls, cfg: ProviderConfig) -> None:
        cls.connection_options.model_validate(cfg.model_extra or {})
        if cls.requires_base_url and not (cfg.base_url or cls.default_base_url):
            raise ValueError(f"Provider {cfg.kind} requires base_url")

    def validate_credentials(self) -> None:
        if self._credentials is not None:
            if not self.api_key() and (self.requires_api_key or self.api_key_env):
                raise RuntimeError(f"Provider {self.cfg.kind} has no configured credential")
            return
        if self.api_key_env:
            if not os.environ.get(self.api_key_env, "").strip():
                raise RuntimeError(
                    f"Environment variable {self.api_key_env} ({self.cfg.kind} API key) is not set"
                )
        elif self.requires_api_key:
            raise RuntimeError(f"Provider {self.cfg.kind} requires api_key_env")

    def api_key(self) -> str | None:
        """Read an injected snapshot, or retain the default environment behavior."""
        if self._credentials is not None:
            return self._credentials[0]
        return os.environ.get(self.api_key_env) if self.api_key_env else None

    def generate(
        self, messages: Messages, model: ResolvedModel, *, json_mode: bool, context: RequestContext
    ) -> str:
        def request() -> str:
            with context.attempt_scope():
                return self._request(messages, model, json_mode=json_mode, context=context)

        return self._generate_with_retry(request, context=context)

    def _generate_with_retry(self, request: Callable[[], str], *, context: RequestContext) -> str:
        """Keep one transient retry budget, even when an adapter negotiates a wire format."""
        reporter = RetryReporter(
            provider=self.cfg.kind,
            tier=context.tier,
            stage=context.operation,
            max_attempts=self.cfg.max_retries + 1,
            emit=context.emit,
            redact=context.redact_diagnostic,
        )

        return provider_retry(self.cfg.max_retries, reporter, sleep=context.sleep)(request)()

    @abstractmethod
    def _request(
        self, messages: Messages, model: ResolvedModel, *, json_mode: bool, context: RequestContext
    ) -> str:
        raise NotImplementedError

    def list_models(self) -> list[str]:
        """Return model names the endpoint offers; adapters without a listing API return none."""
        raise NotImplementedError(f"Provider {self.cfg.kind} cannot list models")

    def json_probe_candidates(self, model: ResolvedModel) -> tuple[tuple[str, ResolvedModel], ...]:
        """Describe probe choices without exposing adapter-specific options to the caller."""
        return (("native", model),)

    def is_json_mode_unsupported(self, error: Exception) -> bool:
        """Only an adapter may recognize its protocol's explicit format rejection."""
        return False

    def close(self) -> None:
        """Release a short-lived probe's SDK connection, if one was created."""
        close = getattr(self._client, "close", None)
        if callable(close):
            close()

    @classmethod
    def output_limit(cls, options: BaseModel, hint: int | None, explicit: int | None) -> int | None:
        return explicit if explicit is not None else hint
