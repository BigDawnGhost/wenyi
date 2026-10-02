"""Request and connection options shared by every profile-driven wire adapter.

Adapters of one wire protocol read the same knobs, so these models live outside the
protocol modules. ``ProviderOptions`` is a per-model request profile: ``thinking`` and
``reasoning_effort`` are placed on the wire by the provider profile's reasoning builder,
while ``temperature`` and ``extra_body`` pass through to the request body unchanged.
``thinking = None`` defers to the profile default, so a provider can change its default
without every model profile being rewritten.

``WireConnectionOptions`` is per-connection. ``reasoning_style`` selects the placement for
OpenAI-compatible endpoints whose dialect is not declared by a provider profile, keeping the
existing escape hatch for custom relays, Ollama, vLLM and llama.cpp servers.
"""

from __future__ import annotations

from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

ReasoningStyle = Literal["none", "deepseek", "openai", "openrouter"]


class ProviderOptions(BaseModel):
    """Vendor-neutral model request options for profile-driven providers."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    thinking: bool | None = None
    reasoning_effort: str | None = None
    temperature: float | None = Field(default=None, ge=0, le=2, allow_inf_nan=False)
    extra_body: dict[str, Any] = Field(default_factory=dict)

    def thinking_enabled(self, default: bool) -> bool:
        """Resolve the thinking toggle against the provider profile default."""
        return default if self.thinking is None else self.thinking


class WireConnectionOptions(BaseModel):
    """Connection-level options for profile-driven providers."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    reasoning_style: ReasoningStyle = "none"


OptionsT = TypeVar("OptionsT", bound=BaseModel)

__all__ = ["OptionsT", "ProviderOptions", "ReasoningStyle", "WireConnectionOptions"]
