"""Explicit provider registration shared by validation, previews and construction."""

from __future__ import annotations

import importlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from .operation_names import OPERATION_NAMES

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

    def validate_model(self, model: ModelConfig, *, options: dict[str, Any] | None = None):
        _check_options(model.options)
        return self.options_type().model_validate(model.options if options is None else options)

    def preset(self) -> dict[str, Any]:
        factory = getattr(self._module(), "preset_model", None)
        if factory is None:
            raise ValueError(
                f"Provider {self.kind!r} has no preset; configure explicit model profiles"
            )
        model = factory()
        reference = f"{self.kind}/{model.model}"
        return {
            "providers": {self.kind: {"kind": self.kind}},
            "models": {reference: {"options": model.options.model_dump()}},
            "routes": {operation: {"model": reference} for operation in OPERATION_NAMES},
        }


def register_providers(specs: Iterable[ProviderSpec]) -> Mapping[str, ProviderSpec]:
    registry: dict[str, ProviderSpec] = {}
    for spec in specs:
        if spec.kind in registry:
            raise ValueError(f"Duplicate provider: {spec.kind}")
        registry[spec.kind] = spec
    return MappingProxyType(registry)


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
        ProviderSpec("fake", "fake", "FakeProvider", "FakeOptions"),
    )
)


def provider_spec(kind: str) -> ProviderSpec:
    try:
        return PROVIDERS[kind]
    except KeyError:
        raise ValueError(f"Unknown provider: {kind}; available: {', '.join(PROVIDERS)}") from None
