"""OpenAI Responses wire protocol for profile-driven providers.

One request shape serves providers that expose the Responses API: instructions move out of the
conversation, input items carry typed text parts, and reasoning arrives as a ``reasoning``
object. The endpoint is read as a server-sent event stream when it advertises one, and as a
single JSON response otherwise, so an endpoint that ignores ``stream`` still works.

Vendor differences stay declarative: static body fields come from ``ProviderProfile.default_body``
(for example the Codex endpoint's ``store`` and ``include``), the header set from the profile,
and the prompt-cache key is only sent when the profile advertises support.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ...retrying import EmptyResponseError, TruncatedResponseError
from ...transport import Messages, RequestContext, ResolvedModel
from .._oauth import iter_sse_json, openai_style_usage, record
from .._openai_compatible import json_instruction_messages
from .._options import ProviderOptions
from .base import ProfileAdapter, is_event_stream, merge_extra_body

RESPONSES_PATH = "/responses"
MAX_FAILURE_CHARS = 400
TERMINAL_EVENTS = frozenset({"response.completed", "response.incomplete", "response.failed"})
FAILURE_EVENTS = frozenset({"response.error", "error"})


def build_input(messages: Messages) -> tuple[list[dict[str, Any]], str | None]:
    """Split system prompts into instructions and the rest into typed input items."""
    instructions: list[str] = []
    items: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role", "user"))
        content = str(message.get("content", "") or "")
        if role == "system":
            if content.strip():
                instructions.append(content)
            continue
        text_type = "output_text" if role == "assistant" else "input_text"
        items.append(
            {
                "type": "message",
                "role": role,
                "content": [{"type": text_type, "text": content}],
            }
        )
    return items, ("\n\n".join(instructions) or None)


def message_text(items: Any) -> str:
    """Read assistant text from the message items of a Responses ``output`` array."""
    source = items if isinstance(items, Sequence) and not isinstance(items, str) else []
    parts: list[str] = []
    for item in source:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        parts.extend(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "output_text"
        )
    return "".join(parts)


def message_refusal(items: Any) -> str | None:
    """Read the refusal text a provider returns instead of an answer."""
    source = items if isinstance(items, Sequence) and not isinstance(items, str) else []
    for item in source:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == "refusal":
                refusal = _text(part.get("refusal"))
                if refusal:
                    return refusal
    return None


def reconcile_text(current: str, final: str | None) -> str:
    """Reconcile streamed deltas with a final snapshot that may lag behind them."""
    if not final:
        return current
    if not current:
        return final
    if final.startswith(current):
        return final
    return current


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _failure_detail(payload: Mapping[str, Any]) -> str | None:
    error = payload.get("error")
    if isinstance(error, dict):
        return _text(error.get("message")) or _short_json(error)
    if error:
        return str(error)
    return None


def _short_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)[:MAX_FAILURE_CHARS]


@dataclass
class ResponsesOutcome:
    """Accumulated text, usage and terminal state of one Responses exchange."""

    text: str = ""
    usage: dict[str, Any] | None = None
    refusal: str | None = None
    failure: str | None = None
    truncated: bool = False

    def apply_payload(self, payload: Mapping[str, Any]) -> None:
        """Apply a complete response object, streamed or read from a JSON body."""
        failure = _failure_detail(payload)
        if failure is not None:
            self.failure = failure
        status = str(payload.get("status") or "").lower()
        if status == "failed":
            self.failure = self.failure or _short_json(payload)
        elif status == "incomplete":
            self.truncated = True
        usage = payload.get("usage")
        if isinstance(usage, dict):
            self.usage = usage
        output = payload.get("output")
        self.text = reconcile_text(self.text, message_text(output) or None)
        refusal = message_refusal(output)
        if refusal and not self.text.strip():
            self.refusal = refusal

    def apply_event(self, event: Mapping[str, Any]) -> None:
        """Apply one decoded server-sent event."""
        event_type = str(event.get("type") or "")
        if event_type == "response.output_text.delta":
            self.text += str(event.get("delta") or "")
        elif event_type == "response.output_text.done":
            self.text = reconcile_text(self.text, _text(event.get("text")))
        elif event_type == "response.refusal.delta":
            self.refusal = (self.refusal or "") + str(event.get("delta") or "")
        elif event_type == "response.output_item.done":
            item = event.get("item")
            items = [item] if isinstance(item, dict) else []
            self.text = reconcile_text(self.text, message_text(items) or None)
            refusal = message_refusal(items)
            if refusal and not self.text.strip():
                self.refusal = refusal
        elif event_type in TERMINAL_EVENTS:
            payload = event.get("response")
            if isinstance(payload, dict):
                self.apply_payload(payload)
            elif event_type == "response.incomplete":
                self.truncated = True
            else:
                self.failure = self.failure or _short_json(event)
        elif event_type in FAILURE_EVENTS:
            self.failure = self.failure or _short_json(event)

    def answer(self, provider: str) -> str:
        """Return the answer text, refusing failed, truncated, refused or empty completions."""
        if self.failure is not None:
            raise RuntimeError(f"{provider} response failed: {self.failure}")
        if not self.text.strip() and self.refusal:
            raise EmptyResponseError(f"{provider} refused the request: {self.refusal}")
        if self.truncated:
            raise TruncatedResponseError(f"{provider} response was truncated at its output limit")
        if not self.text.strip():
            raise EmptyResponseError(f"{provider} response content is empty")
        return self.text


class ResponsesClient(ProfileAdapter[ProviderOptions]):
    """Post one Responses body and read either its event stream or its JSON response."""

    def __init__(self, cfg):
        super().__init__(cfg)
        # Pin requests to one cache shard so prompt prefix caching stays effective.
        self.cache_key = uuid.uuid4().hex

    def build_body(
        self,
        messages: Messages,
        model: ResolvedModel[ProviderOptions],
        *,
        json_mode: bool,
        context: RequestContext,
    ) -> dict[str, Any]:
        """Build the Responses request body for one attempt."""
        options = model.options
        prepared = json_instruction_messages(messages) if json_mode else messages
        items, instructions = build_input(prepared)
        body: dict[str, Any] = {"model": model.model, "input": items, "stream": True}
        if instructions is not None:
            body["instructions"] = instructions
        if options.temperature is not None:
            body["temperature"] = options.temperature
        if context.max_tokens is not None:
            body["max_output_tokens"] = context.max_tokens
        if self.profile.supports_prompt_cache_key:
            body["prompt_cache_key"] = self.cache_key
        merge_extra_body(body, self.profile.default_body)
        extra_body, top_level = self.reasoning_parts(model.model, options)
        merge_extra_body(body, top_level, extra_body, options.extra_body)
        return body

    def read_outcome(self, body: dict[str, Any]) -> ResponsesOutcome:
        """Send one request and accumulate its stream or its JSON response."""
        outcome = ResponsesOutcome()
        with self.open_stream(RESPONSES_PATH, body) as response:
            if is_event_stream(response):
                terminal = False
                for event in iter_sse_json(response.iter_lines()):
                    outcome.apply_event(event)
                    terminal = terminal or event.get("type") in TERMINAL_EVENTS | FAILURE_EVENTS
                if not terminal:
                    outcome.truncated = True
            else:
                outcome.apply_payload(self.json_object(response))
        return outcome

    def _request(
        self,
        messages: Messages,
        model: ResolvedModel[ProviderOptions],
        *,
        json_mode: bool,
        context: RequestContext,
    ) -> str:
        outcome = self.read_outcome(
            self.build_body(messages, model, json_mode=json_mode, context=context)
        )
        record(context, openai_style_usage(outcome.usage))
        return outcome.answer(self.profile.display())


__all__ = [
    "RESPONSES_PATH",
    "ResponsesClient",
    "ResponsesOutcome",
    "build_input",
    "message_refusal",
    "message_text",
    "reconcile_text",
]
