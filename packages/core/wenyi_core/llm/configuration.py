"""Typed routing configuration; adapters validate their own protocol options."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .operations import TIERS, require_operation


class ProviderConfig(BaseModel):
    """Connection envelope; the provider registry validates adapter-owned extra fields."""

    model_config = ConfigDict(extra="allow", frozen=True, hide_input_in_errors=True)
    kind: str
    base_url: str | None = None
    api_key_env: str | None = None
    timeout: float = Field(default=600, gt=0, allow_inf_nan=False)
    max_retries: int = Field(default=4, ge=0)
    max_concurrency: int | None = Field(default=None, gt=0)
    quota_group: str | None = None

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, value: str | None) -> str | None:
        if value is not None:
            url = urlsplit(value)
            if url.scheme not in {"http", "https"} or not url.hostname:
                raise ValueError("base_url must be an absolute HTTP(S) endpoint")
            if url.username or url.password or url.query or url.fragment:
                raise ValueError(
                    "base_url cannot contain credentials, query parameters or fragments"
                )
        return value

    @field_validator("api_key_env")
    @classmethod
    def validate_env_name(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise ValueError("api_key_env must be an environment variable name")
        return value


class ModelConfig(BaseModel):
    """A reusable model request profile referencing one connection."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    max_output_tokens: int | None = Field(default=None, gt=0)
    options: dict[str, Any] = Field(default_factory=dict)

    @field_validator("model", "provider")
    @classmethod
    def nonblank_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Model and provider names cannot be blank")
        return value


class RouteConfig(BaseModel):
    """Select exactly one model profile or product tier."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    model: str | None = None
    tier: str | None = None
    fallbacks: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_selection(self) -> RouteConfig:
        if (self.model is None) == (self.tier is None):
            raise ValueError("A route requires exactly one of model or tier")
        if self.tier is not None and self.tier not in TIERS:
            raise ValueError(f"Unknown tier: {self.tier}")
        return self


class QuotaConfig(BaseModel):
    """Optional account-group limits shared by connections in this process."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    requests_per_minute: int | None = Field(default=None, gt=0)
    tokens_per_minute: int | None = Field(default=None, gt=0)


class BudgetConfig(BaseModel):
    """Optional invocation limits; token reservations use conservative estimates."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    max_requests: int | None = Field(default=None, gt=0)
    max_tokens: int | None = Field(default=None, gt=0)
    deadline_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)


class LLMConfig(BaseModel):
    """One routing schema for both presets and fully explicit configurations."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    preset: str | None = None
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    models: dict[str, ModelConfig] = Field(default_factory=dict)
    tiers: dict[str, str] = Field(default_factory=dict)
    routes: dict[str, RouteConfig] = Field(default_factory=dict)
    quotas: dict[str, QuotaConfig] = Field(default_factory=dict)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)

    @model_validator(mode="before")
    @classmethod
    def expand_preset(cls, raw: Any) -> Any:
        if not isinstance(raw, dict):
            return raw
        raw = dict(raw)
        if not raw:
            raw["preset"] = "deepseek"
        preset = raw.get("preset")
        if preset is not None:
            from .registry import provider_spec

            defaults = provider_spec(preset).preset()
            for section in ("providers", "models", "tiers"):
                supplied = raw.get(section, {})
                if not isinstance(supplied, dict):
                    raise ValueError(f"llm.{section} must be a mapping")
                raw[section] = {**defaults[section], **supplied}
        return raw

    @model_validator(mode="after")
    def validate_graph(self) -> LLMConfig:
        from .registry import provider_spec

        for section in ("providers", "models", "quotas"):
            for name in getattr(self, section):
                if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]*", name):
                    raise ValueError(f"Invalid llm.{section} ID: {name}")
        if set(self.tiers) != set(TIERS):
            raise ValueError("llm.tiers must define exactly strong, cheap and fast")
        for name, connection in self.providers.items():
            provider_spec(connection.kind).validate_connection(connection)
            if connection.quota_group and connection.quota_group not in self.quotas:
                raise ValueError(f"llm.providers.{name}.quota_group: unknown quota group")
        for name, model in self.models.items():
            if model.provider not in self.providers:
                raise ValueError(
                    f"llm.models.{name}.provider: unknown connection {model.provider!r}"
                )
            try:
                provider = provider_spec(self.providers[model.provider].kind)
                options = provider.validate_model(model)
                provider.adapter_type().output_limit(options, None, model.max_output_tokens)
            except ValueError as error:
                raise ValueError(f"llm.models.{name}: {error}") from error
        for name, reference in self.tiers.items():
            if reference not in self.models:
                raise ValueError(f"llm.tiers.{name}: unknown model profile {reference!r}")
        for operation, route in self.routes.items():
            require_operation(operation)
            for reference in ([route.model] if route.model is not None else []) + route.fallbacks:
                if reference not in self.models:
                    raise ValueError(
                        f"llm.routes.{operation}.model: unknown model profile {reference!r}"
                    )
            primary = route.model or self.tiers[route.tier or "strong"]
            if len(set([primary, *route.fallbacks])) != 1 + len(route.fallbacks):
                raise ValueError(f"llm.routes.{operation}: duplicate fallback model")
        from .routing import resolve_routes

        resolve_routes(self)
        return self
