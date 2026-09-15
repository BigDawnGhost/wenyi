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


class ProviderAdapter(ABC):
    """Own an SDK connection; the shared retry helper wraps each guarded transport attempt."""

    default_base_url: str | None = None
    default_api_key_env: str | None = None
    requires_api_key = False
    requires_base_url = True
    protocol_version = 1
    connection_options: type[BaseModel] = ConnectionOptions

    def __init__(self, cfg: ProviderConfig):
        self.validate_connection(cfg)
        self.cfg = cfg
        self.base_url = cfg.base_url or self.default_base_url
        self.api_key_env = cfg.api_key_env or self.default_api_key_env
        self._client: Any = None
        self._client_lock = threading.Lock()

    @classmethod
    def validate_connection(cls, cfg: ProviderConfig) -> None:
        cls.connection_options.model_validate(cfg.model_extra or {})
        if cls.requires_base_url and not (cfg.base_url or cls.default_base_url):
            raise ValueError(f"Provider {cfg.kind} requires base_url")

    def validate_credentials(self) -> None:
        if self.api_key_env:
            if not os.environ.get(self.api_key_env, "").strip():
                raise RuntimeError(
                    f"Environment variable {self.api_key_env} ({self.cfg.kind} API key) is not set"
                )
        elif self.requires_api_key:
            raise RuntimeError(f"Provider {self.cfg.kind} requires api_key_env")

    def generate(
        self, messages: Messages, model: ResolvedModel, *, json_mode: bool, context: RequestContext
    ) -> str:
        reporter = RetryReporter(
            provider=self.cfg.kind,
            tier=context.tier,
            stage=context.operation,
            max_attempts=self.cfg.max_retries + 1,
            emit=context.emit,
        )

        @provider_retry(self.cfg.max_retries, reporter, sleep=context.sleep)
        def request() -> str:
            with context.attempt_scope():
                return self._request(messages, model, json_mode=json_mode, context=context)

        return request()

    @abstractmethod
    def _request(
        self, messages: Messages, model: ResolvedModel, *, json_mode: bool, context: RequestContext
    ) -> str:
        raise NotImplementedError

    @classmethod
    def output_limit(cls, options: BaseModel, hint: int | None, explicit: int | None) -> int | None:
        return explicit if explicit is not None else hint
