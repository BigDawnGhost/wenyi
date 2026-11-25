"""Typed routing configuration; adapters validate their own protocol options."""

from __future__ import annotations

import re
from typing import Annotated, Any
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_core import InitErrorDetails

from .operation_names import OPERATION_NAMES, require_operation_name


def _located_error(error: ValueError, location: tuple[str, ...]) -> ValidationError:
    """Keep nested validation locations so saved-setting reconstruction can prune one field."""
    details: list[InitErrorDetails] = []
    if isinstance(error, ValidationError):
        for detail in error.errors(include_url=False):
            details.append(
                InitErrorDetails(
                    type=detail["type"],
                    loc=(*location, *detail["loc"]),
                    input=detail.get("input"),
                    ctx=detail.get("ctx", {}),
                )
            )
    else:
        details.append(
            InitErrorDetails(type="value_error", loc=location, input=None, ctx={"error": error})
        )
    return ValidationError.from_exception_data("LLMConfig", details, hide_input=True)


def merge_llm_settings(base: dict, override: dict) -> dict:
    """Merge a configuration layer by key; fields the layer omits inherit from base.

    Nested dictionaries merge recursively so one layer can override a single
    field without discarding sibling fields. Every other value - including
    lists and explicit None - replaces the base value wholesale.
    """
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_llm_settings(result[key], value)
        else:
            result[key] = value
    return result


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
    """Request parameters for one connection/model pair."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    max_output_tokens: int | None = Field(default=None, gt=0)
    context_window: int | None = Field(default=None, gt=0)
    options: dict[str, Any] = Field(default_factory=dict)


def split_model_ref(reference: str) -> tuple[str, str]:
    """Only the first slash is structural; remote model names may contain slashes."""
    provider, _, model = reference.partition("/")
    if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]*", provider) or not model or model != model.strip():
        raise ValueError(f"Expected connection/model, got {reference!r}")
    return provider, model


def _model_ref(reference: str) -> str:
    split_model_ref(reference)
    return reference


ModelRef = Annotated[str, AfterValidator(_model_ref)]


def effective_context_window(provider_kind: str, model: str, configured: int | None) -> int | None:
    """Return explicit ``context_window``, or a conservative documented default when known."""
    if configured is not None:
        return configured
    kind = provider_kind.lower()
    name = model.lower()
    if kind == "deepseek":
        return 128_000
    if kind == "gemini" and name.startswith(("gemini-1.5", "gemini-2", "gemini-3")):
        return 1_000_000
    if kind == "openai":
        if name.startswith(("o1", "o3", "o4", "gpt-5")):
            return 200_000
        if name.startswith(("gpt-4o", "gpt-4-turbo", "gpt-4.1")):
            return 128_000
    return None


class RouteConfig(BaseModel):
    """Select a registered model and optional ordered fallback profiles."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    model: ModelRef
    fallbacks: list[ModelRef] = Field(default_factory=list)
    thinking: str | None = None


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
    token_estimate_divisor: float = Field(default=2.0, gt=0, allow_inf_nan=False)


class LLMConfig(BaseModel):
    """One routing schema for both presets and fully explicit configurations."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    preset: str | None = None
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    models: dict[ModelRef, ModelConfig] = Field(default_factory=dict)
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
            if not isinstance(preset, str):
                raise _located_error(ValueError("llm.preset must be a string or null"), ("preset",))
            from .registry import provider_spec

            try:
                defaults = provider_spec(preset).preset()
            except ValueError as error:
                raise _located_error(error, ("preset",)) from error
            for section in ("providers", "models", "routes"):
                supplied = raw.get(section, {})
                if not isinstance(supplied, dict):
                    raise _located_error(ValueError(f"llm.{section} must be a mapping"), (section,))
                raw[section] = {**defaults[section], **supplied}
        return raw

    @model_validator(mode="after")
    def validate_graph(self) -> LLMConfig:
        from .registry import provider_spec

        for section in ("providers", "quotas"):
            for name in getattr(self, section):
                if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]*", name):
                    raise _located_error(
                        ValueError(f"Invalid llm.{section} ID: {name}"), (section, name)
                    )
        for name, connection in self.providers.items():
            try:
                provider = provider_spec(connection.kind)
            except ValueError as error:
                raise _located_error(error, ("providers", name, "kind")) from error
            try:
                provider.validate_connection(connection)
            except ValueError as error:
                raise _located_error(error, ("providers", name)) from error
            if connection.quota_group and connection.quota_group not in self.quotas:
                raise _located_error(
                    ValueError(f"llm.providers.{name}.quota_group: unknown quota group"),
                    ("providers", name, "quota_group"),
                )
        for name, model in self.models.items():
            connection, _ = split_model_ref(name)
            if connection not in self.providers:
                raise _located_error(
                    ValueError(f"llm.models.{name}: unknown connection {connection!r}"),
                    ("models", name),
                )
            try:
                provider = provider_spec(self.providers[connection].kind)
                options = provider.validate_model(model)
                provider.adapter_type().output_limit(options, None, model.max_output_tokens)
            except ValidationError as error:
                raise _located_error(error, ("models", name, "options")) from error
            except ValueError as error:
                raise _located_error(
                    ValueError(f"llm.models.{name}: {error}"), ("models", name)
                ) from error
        for operation, route in self.routes.items():
            try:
                require_operation_name(operation)
            except ValueError as error:
                raise _located_error(error, ("routes", operation)) from error
            selections = [route.model, *route.fallbacks]
            for reference in selections:
                if reference not in self.models:
                    raise _located_error(
                        ValueError(
                            f"llm.routes.{operation}.model: unknown model profile {reference!r}"
                        ),
                        ("routes", operation),
                    )
            if len(set(selections)) != len(selections):
                raise _located_error(
                    ValueError(f"llm.routes.{operation}: duplicate fallback model"),
                    ("routes", operation, "fallbacks"),
                )
        if missing := set(OPERATION_NAMES) - self.routes.keys():
            raise ValidationError.from_exception_data(
                "LLMConfig",
                [
                    InitErrorDetails(type="missing", loc=("routes", operation), input=None)
                    for operation in sorted(missing)
                ],
                hide_input=True,
            )
        from .routing import resolve_routes

        resolve_routes(self)
        return self
