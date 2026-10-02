"""Explicit provider registration shared by validation, previews and construction.

Two kinds of providers are registered here. Vendors with protocol code of their own get a
dedicated adapter module; every declarative profile is routed through the wire adapter that
speaks its protocol, so adding a provider is one entry in ``profiles.py``.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from .profiles import PROFILES, get_profile
from .providers.wire.base import PROFILE_WIRES, WIRE_ADAPTERS

if TYPE_CHECKING:
    from .configuration import ModelConfig, ProviderConfig


def _check_options(value: Any) -> None:
    """Keep routing, credentials and protocol ownership out of raw request overrides."""
    protected = {
        "api_key",
        "apikey",
        "api_token",
        "access_token",
        "authorization",
        "headers",
        "extra_headers",
        "cookies",
        "model",
        "messages",
        "contents",
        "system_instruction",
        "stream",
        "max_tokens",
        "max_completion_tokens",
        "max_output_tokens",
        "response_format",
        "response_mime_type",
        "base_url",
        "http_options",
        "client_args",
        "retry_options",
        "max_retries",
        "timeout",
    }
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower().replace("_", "").replace("-", "")
            if normalized in {name.replace("_", "") for name in protected}:
                raise ValueError(f"Reserved model option: {key}")
            _check_options(child)
    elif isinstance(value, list):
        for child in value:
            _check_options(child)


@dataclass(frozen=True)
class ProviderSpec:
    """Describe one adapter without constructing SDK clients during validation."""

    kind: str
    module: str
    client_class: str
    options_class: str
    options_module: str | None = None

    def _module(self):
        return importlib.import_module(f"wenyi_core.llm.providers.{self.module}")

    def adapter_type(self):
        return getattr(self._module(), self.client_class)

    def options_type(self):
        module = importlib.import_module(
            f"wenyi_core.llm.providers.{self.options_module or self.module}"
        )
        return getattr(module, self.options_class)

    def validate_connection(self, connection: ProviderConfig) -> None:
        self.adapter_type().validate_connection(connection)

    def validate_model(self, model: ModelConfig):
        _check_options(model.options)
        return self.options_type().model_validate(model.options)

    def endpoint(self, connection: ProviderConfig) -> str | None:
        """Resolve the endpoint a connection will use, without constructing the adapter."""
        resolver = getattr(self.adapter_type(), "endpoint", None)
        if resolver is not None:
            return resolver(connection)
        return connection.base_url or self.adapter_type().default_base_url

    def output_limit(self, options: Any, hint: int | None, explicit: int | None) -> int | None:
        """Resolve the output cap for one model profile."""
        return self.adapter_type().output_limit(options, hint, explicit)

    def preset(self) -> dict[str, Any]:
        factory = getattr(self._module(), "preset_models", None)
        if factory is None:
            raise ValueError(
                f"Provider {self.kind!r} has no preset; configure explicit model profiles"
            )
        definitions = factory()
        return {
            "providers": {"default": {"kind": self.kind}},
            "models": {
                f"default_{tier}": {
                    "provider": "default",
                    "model": model.model,
                    "options": model.options.model_dump(),
                }
                for tier, model in definitions.items()
            },
            "tiers": {tier: f"default_{tier}" for tier in definitions},
        }


@dataclass(frozen=True)
class ProfileSpec(ProviderSpec):
    """Route one declarative provider profile through the wire adapter that speaks it."""

    def preset(self) -> dict[str, Any]:
        definitions = get_profile(self.kind).preset_models()
        if not definitions:
            raise ValueError(
                f"Provider {self.kind!r} has no preset; configure explicit model profiles"
            )
        return {
            "providers": {"default": {"kind": self.kind}},
            "models": {
                f"default_{tier}": {"provider": "default", "model": model, "options": {}}
                for tier, model in definitions.items()
            },
            "tiers": {tier: f"default_{tier}" for tier in definitions},
        }

    def endpoint(self, connection: ProviderConfig) -> str | None:
        """Resolve the endpoint from the connection, then from the provider profile."""
        return self.adapter_type().endpoint(connection)

    def output_limit(self, options: Any, hint: int | None, explicit: int | None) -> int | None:
        """Resolve the output cap from the defaults the provider profile declares."""
        return self.adapter_type().output_limit(
            options, hint, explicit, profile=get_profile(self.kind)
        )


def register_providers(specs: Iterable[ProviderSpec]) -> Mapping[str, ProviderSpec]:
    registry: dict[str, ProviderSpec] = {}
    for spec in specs:
        if spec.kind in registry:
            raise ValueError(f"Duplicate provider: {spec.kind}")
        registry[spec.kind] = spec
    return MappingProxyType(registry)


# Providers whose protocol needs code beyond the declarative profiles.
DEDICATED_KINDS = frozenset(
    {
        "deepseek",
        "openai",
        "openrouter",
        "opencode-go",
        "openai-compatible",
        "orcarouter",
        "ollama",
        "vllm",
        "gemini",
        "openai-codex",
        "kimi-coding",
        "kimi-coding-cn",
        "copilot",
        "fake",
    }
)


def profile_specs() -> Iterator[ProfileSpec]:
    """Yield one spec for every declarative profile that has no dedicated adapter."""
    for profile in PROFILES:
        if profile.kind in DEDICATED_KINDS or profile.wire not in PROFILE_WIRES:
            continue
        module, client_class = WIRE_ADAPTERS[profile.wire]
        yield ProfileSpec(profile.kind, module, client_class, "ProviderOptions", "_options")


PROVIDERS = register_providers(
    (
        ProviderSpec("deepseek", "deepseek", "DeepSeekClient", "DeepSeekOptions"),
        ProviderSpec("openai", "openai", "OpenAIClient", "OpenAIOptions"),
        ProviderSpec("openrouter", "openrouter", "OpenRouterClient", "OpenRouterOptions"),
        ProviderSpec("opencode-go", "opencode_go", "OpenCodeGoClient", "OpenCodeGoOptions"),
        ProviderSpec(
            "openai-compatible",
            "openai_compatible",
            "OpenAICompatibleClient",
            "OpenAICompatibleOptions",
        ),
        ProviderSpec(
            "orcarouter",
            "orcarouter",
            "OrcaRouterClient",
            "OpenAICompatibleOptions",
            "openai_compatible",
        ),
        ProviderSpec(
            "ollama", "ollama", "OllamaClient", "OpenAICompatibleOptions", "openai_compatible"
        ),
        ProviderSpec("vllm", "vllm", "VLLMClient", "OpenAICompatibleOptions", "openai_compatible"),
        ProviderSpec("gemini", "gemini", "GeminiClient", "GeminiOptions"),
        ProviderSpec("openai-codex", "codex", "CodexClient", "ProviderOptions", "_options"),
        ProfileSpec("kimi-coding", "kimi", "KimiClient", "ProviderOptions", "_options"),
        ProfileSpec("kimi-coding-cn", "kimi", "KimiClient", "ProviderOptions", "_options"),
        ProfileSpec("copilot", "copilot", "CopilotClient", "ProviderOptions", "_options"),
        ProviderSpec("fake", "fake", "FakeProvider", "FakeOptions"),
        *profile_specs(),
    )
)


@dataclass(frozen=True)
class ProviderSummary:
    """One routable provider described for humans; no credential or request is touched."""

    kind: str
    display_name: str
    wire: str | None
    auth: str | None
    credential_envs: tuple[str, ...]
    base_url: str | None
    aliases: tuple[str, ...]
    sign_in: str | None
    models: tuple[str, ...]
    notes: str


def provider_catalog() -> tuple[ProviderSummary, ...]:
    """Describe every registered provider from its profile, without contacting anything."""
    from .configuration import ProviderConfig
    from .providers._credentials import candidate_env_vars
    from .subscriptions import SUBSCRIPTIONS, SUBSCRIPTIONS_BY_KIND

    login_names = {id(subscription): name for name, subscription in SUBSCRIPTIONS.items()}
    summaries: list[ProviderSummary] = []
    for kind, spec in PROVIDERS.items():
        try:
            profile = get_profile(kind)
        except ValueError:
            adapter = spec.adapter_type()
            summaries.append(
                ProviderSummary(
                    kind=kind,
                    display_name=kind,
                    wire=None,
                    auth=None,
                    credential_envs=(
                        (adapter.default_api_key_env,) if adapter.default_api_key_env else ()
                    ),
                    base_url=adapter.default_base_url,
                    aliases=(),
                    sign_in=None,
                    models=(),
                    notes="Dedicated adapter without a declarative profile.",
                )
            )
            continue
        subscription = SUBSCRIPTIONS_BY_KIND.get(profile.kind)
        summaries.append(
            ProviderSummary(
                kind=profile.kind,
                display_name=profile.display(),
                wire=profile.wire,
                auth=profile.auth,
                credential_envs=candidate_env_vars(profile),
                base_url=spec.endpoint(ProviderConfig(kind=profile.kind)),
                aliases=profile.aliases,
                sign_in=login_names.get(id(subscription)) if subscription is not None else None,
                models=profile.fallback_models,
                notes=profile.notes,
            )
        )
    return tuple(summaries)


def provider_spec(kind: str) -> ProviderSpec:
    from .profiles import PROFILE_INDEX

    normalized = kind.strip().lower()
    profile = PROFILE_INDEX.get(normalized)
    canonical = profile.kind if profile is not None else normalized
    try:
        return PROVIDERS[canonical]
    except KeyError:
        raise ValueError(f"Unknown provider: {kind}; available: {', '.join(PROVIDERS)}") from None
