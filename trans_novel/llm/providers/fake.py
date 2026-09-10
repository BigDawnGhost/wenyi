"""Offline workflow client and provider adapter for deterministic tests."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict

from ..base import LLMClient, Messages
from ..configuration import LLMConfig
from ..transport import ProviderAdapter, RequestContext, ResolvedModel


class FakeOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


def preset_models() -> dict[str, ResolvedModel[FakeOptions]]:
    return {tier: ResolvedModel("fake", FakeOptions()) for tier in ("strong", "cheap", "fast")}


class FakeProvider(ProviderAdapter):
    """A transport with injectable behavior and no SDK or credentials."""

    requires_base_url = False

    def _request(
        self, messages: Messages, model: ResolvedModel, *, json_mode: bool, context: RequestContext
    ) -> str:
        return "[]" if json_mode else ""


class FakeClient(LLMClient):
    """Inject directly into workflows; record immutable request snapshots."""

    def __init__(
        self,
        handler: Callable[[Messages, str, bool], str] | None = None,
        *,
        config: LLMConfig | None = None,
    ) -> None:
        super().__init__()
        from ..routing import resolve_routes

        self.handler = handler
        self.config = config or LLMConfig.model_validate({"preset": "fake"})
        self.routes = resolve_routes(self.config)
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        messages: Messages,
        *,
        operation: str,
        json_mode: bool = False,
        max_tokens: int | None = None,
    ) -> str:
        from ..operations import require_operation
        from ..routing import model_route

        require_operation(operation)
        route = self.routes[operation]
        if max_tokens is not None:
            route = model_route(
                self.config,
                operation,
                route.profile,
                origin=route.origin,
                tier=route.tier,
                output_hint=max_tokens,
            )
        tier = route.tier or "direct"
        self.calls.append(
            {
                "messages": [dict(message) for message in messages],
                "operation": operation,
                "stage": operation,
                "tier": tier,
                "json_mode": json_mode,
                "max_tokens": route.max_output_tokens,
                "model": route.model,
                "provider": route.provider,
            }
        )
        if self.handler is not None:
            return self.handler(messages, tier, json_mode)
        return "[]" if json_mode else ""
