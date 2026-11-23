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


def preset_model() -> ResolvedModel[FakeOptions]:
    return ResolvedModel("fake", FakeOptions())


class FakeProvider(ProviderAdapter):
    """A transport with injectable behavior and no SDK or credentials."""

    requires_base_url = False

    def _request(
        self, messages: Messages, model: ResolvedModel, *, json_mode: bool, context: RequestContext
    ) -> str:
        return "[]" if json_mode else ""

    def list_models(self) -> list[str]:
        return ["fake"]


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
        thinking_boost: int = 0,
    ) -> str:
        from ..operations import require_operation
        from ..routing import operation_route

        require_operation(operation)
        route = self.routes[operation]
        if max_tokens is not None:
            route = operation_route(self.config, operation, output_hint=max_tokens)
        self.calls.append(
            {
                "messages": [dict(message) for message in messages],
                "operation": operation,
                "stage": operation,
                "profile": route.profile,
                "json_mode": json_mode,
                "max_tokens": route.max_output_tokens,
                "model": route.model,
                "provider": route.provider,
                "thinking_boost": thinking_boost,
            }
        )
        if self.handler is not None:
            return self.handler(messages, operation, json_mode)
        return "[]" if json_mode else ""
