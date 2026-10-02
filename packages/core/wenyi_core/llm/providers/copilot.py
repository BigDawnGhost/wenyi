"""GitHub Copilot model-specific wire selection and editor attribution."""

from __future__ import annotations

import re

from ..configuration import ProviderConfig
from ..oauth.credentials import OAuthCredentialError
from ..transport import Messages, RequestContext, ResolvedModel
from ._credentials import COPILOT_EDITOR_HEADERS
from ._options import ProviderOptions
from .wire.openai_chat import OpenAIChatClient
from .wire.responses import ResponsesClient


def model_wire(model: str) -> str:
    """Use Responses for GPT-5+, except gpt-5-mini; other families use Chat."""
    name = model.strip().lower().rsplit("/", 1)[-1]
    match = re.match(r"^gpt-(\d+)", name)
    if match and int(match.group(1)) >= 5 and not name.startswith("gpt-5-mini"):
        return "responses"
    return "chat"


class CopilotClient(ResponsesClient, OpenAIChatClient):
    """Select the model's native Copilot protocol without mutable per-request state."""

    def resolve_base_url(self) -> str:
        """Honor explicit overrides, then the endpoint returned for the account."""
        endpoint = self.endpoint(self.cfg)
        if self.cfg.base_url or endpoint != self.profile.base_url:
            return super().resolve_base_url()
        account_endpoint = self.credential().api_base_url
        if account_endpoint:
            try:
                ProviderConfig.validate_url(account_endpoint)
                if not account_endpoint.startswith("https://"):
                    raise ValueError("HTTPS is required")
            except ValueError as error:
                raise OAuthCredentialError(
                    "Copilot account endpoint must be an absolute HTTPS URL without credentials"
                ) from error
            return account_endpoint.rstrip("/")
        return super().resolve_base_url()

    def request_headers(self, *, accept: str = "application/json") -> dict[str, str]:
        return {
            **super().request_headers(accept=accept),
            **COPILOT_EDITOR_HEADERS,
            "openai-intent": "conversation-panel",
            "x-initiator": "user",
        }

    def reasoning_parts(self, model: str, options: ProviderOptions) -> tuple[dict, dict]:
        if model_wire(model) == "responses" and options.thinking_enabled(
            self.profile.default_thinking
        ):
            return {}, {"reasoning": {"effort": options.reasoning_effort or "medium"}}
        return {}, {}

    def _request(
        self,
        messages: Messages,
        model: ResolvedModel[ProviderOptions],
        *,
        json_mode: bool,
        context: RequestContext,
    ) -> str:
        adapter = {
            "responses": ResponsesClient,
            "chat": OpenAIChatClient,
        }[model_wire(model.model)]
        return adapter._request(self, messages, model, json_mode=json_mode, context=context)
