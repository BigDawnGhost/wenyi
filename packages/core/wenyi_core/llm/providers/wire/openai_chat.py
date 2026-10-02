"""OpenAI Chat Completions wire protocol for profile-driven providers.

The request shape is shared with the SDK-based compatible providers; what differs is that
headers, endpoint and reasoning placement all come from the declarative provider profile.
"""

from __future__ import annotations

from typing import Any

from ...retrying import EmptyResponseError, TruncatedResponseError
from ...transport import Messages, RequestContext, ResolvedModel
from .._oauth import record
from .._openai_compatible import base_request_kwargs, normalize_openai_usage
from .._options import ProviderOptions
from ..openai_compatible import dialect_reasoning
from .base import ProfileAdapter, merge_extra_body

CHAT_COMPLETIONS_PATH = "/chat/completions"
DEFAULT_DIALECT_EFFORT = "high"


def completion_text(payload: dict[str, Any], provider: str) -> str:
    """Read the first choice's text, refusing truncated or empty completions."""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise EmptyResponseError(f"{provider} returned no completion choices")
    choice = choices[0] if isinstance(choices[0], dict) else {}
    if str(choice.get("finish_reason") or "").lower() == "length":
        raise TruncatedResponseError(f"{provider} response was truncated at the token limit")
    message = choice.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    text = content if isinstance(content, str) else ""
    if not text.strip():
        raise EmptyResponseError(f"{provider} response content is empty")
    return text


class OpenAIChatClient(ProfileAdapter[ProviderOptions]):
    """Post one Chat Completions body using the profile's headers and reasoning placement."""

    def reasoning_placement(
        self, model: str, options: ProviderOptions
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return ``(extra_body, top_level)`` describing where reasoning controls go.

        A connection-level ``reasoning_style`` wins over the profile so custom
        OpenAI-compatible endpoints keep their dialect escape hatch; otherwise the provider
        profile decides the placement.
        """
        style = self.wire_options.reasoning_style
        if style != "none":
            top_level, extra_body = dialect_reasoning(
                style,
                thinking=options.thinking_enabled(self.profile.default_thinking),
                effort=(
                    options.reasoning_effort
                    or self.profile.default_effort
                    or DEFAULT_DIALECT_EFFORT
                ),
            )
            return extra_body, top_level
        return self.reasoning_parts(model, options)

    def _request(
        self,
        messages: Messages,
        model: ResolvedModel[ProviderOptions],
        *,
        json_mode: bool,
        context: RequestContext,
    ) -> str:
        options = model.options
        body = base_request_kwargs(model.model, messages, json_mode=json_mode)
        extra_body, top_level = self.reasoning_placement(model.model, options)
        merge_extra_body(body, top_level)
        if options.temperature is not None:
            body["temperature"] = options.temperature
        if context.max_tokens is not None:
            body["max_tokens"] = context.max_tokens
        merge_extra_body(body, extra_body, options.extra_body)
        payload = self.post_json(CHAT_COMPLETIONS_PATH, body)
        record(context, normalize_openai_usage(payload.get("usage")))
        return completion_text(payload, self.profile.display())


__all__ = [
    "CHAT_COMPLETIONS_PATH",
    "DEFAULT_DIALECT_EFFORT",
    "OpenAIChatClient",
    "completion_text",
]
