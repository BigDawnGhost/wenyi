"""Google Cloud Code Assist wire protocol for profile-driven providers.

The Cloud Code endpoint wraps a Gemini ``generateContent`` request in an envelope that carries
the Cloud project, the client identity and a credit type. Reasoning depth is not a request
field here: it is part of the model ID, so the profile owns the model names and this adapter
only moves messages and reads the stream.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from ...oauth.credentials import OAuthCredential
from ...retrying import EmptyResponseError, TruncatedResponseError
from ...transport import Messages, RequestContext, ResolvedModel
from .._oauth import gemini_style_usage, iter_sse_json, record
from .._options import ProviderOptions
from .base import ProfileAdapter, is_event_stream, merge_extra_body

STREAM_PATH = "/v1internal:streamGenerateContent?alt=sse"
DEFAULT_PROJECT = "bamboo-precept-lgxtn"
USER_AGENT_IDENTITY = "antigravity"
REQUEST_TYPE = "agent"
CREDIT_TYPE = "GOOGLE_ONE_AI"
MAX_FAILURE_CHARS = 400

TRUNCATED_REASONS = frozenset({"MAX_TOKENS"})
BLOCKED_REASONS = frozenset(
    {
        "SAFETY",
        "RECITATION",
        "BLOCKLIST",
        "PROHIBITED_CONTENT",
        "SPII",
        "IMAGE_SAFETY",
        "MALFORMED_FUNCTION_CALL",
    }
)
# Thinking parts arrive inline in the text stream, wrapped in think tags by some models.
THINK_TAGS = re.compile(r"</?think>", re.IGNORECASE)


def request_id() -> str:
    """Build the per-request identifier the Cloud Code envelope expects."""
    return f"agent/{int(time.time() * 1000)}/{secrets.token_hex(4)}"


def strip_think_tags(text: str) -> str:
    """Remove inline thinking markers from a content part."""
    return THINK_TAGS.sub("", text)


def build_request(messages: Messages) -> dict[str, Any]:
    """Convert OpenAI-style messages into a Gemini ``generateContent`` request body."""
    system_parts: list[str] = []
    contents: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role", "user"))
        content = str(message.get("content", "") or "")
        if role == "system":
            if content.strip():
                system_parts.append(content)
            continue
        contents.append(
            {
                "role": "model" if role == "assistant" else "user",
                "parts": [{"text": content}],
            }
        )
    if not contents:
        contents.append({"role": "user", "parts": [{"text": ""}]})
    request: dict[str, Any] = {"contents": contents}
    if system_parts:
        request["systemInstruction"] = {
            "role": "system",
            "parts": [{"text": "\n\n".join(system_parts)}],
        }
    return request


def generation_config(
    options: ProviderOptions,
    *,
    max_tokens: int | None,
    json_mode: bool,
) -> dict[str, Any]:
    """Build the ``generationConfig`` section for one request."""
    config: dict[str, Any] = {}
    if max_tokens is not None:
        config["maxOutputTokens"] = max_tokens
    if json_mode:
        config["responseMimeType"] = "application/json"
    if options.temperature is not None:
        config["temperature"] = options.temperature
    return config


def _short_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)[:MAX_FAILURE_CHARS]


@dataclass
class CloudCodeOutcome:
    """Accumulated text, usage and terminal state of one Cloud Code exchange."""

    text: str = ""
    usage: dict[str, Any] | None = None
    finish_reason: str | None = None
    block_reason: str | None = None
    failure: str | None = None

    def apply_payload(self, payload: Mapping[str, Any]) -> None:
        """Apply one ``GenerateContentResponse``, streaming or complete."""
        error = payload.get("error")
        if isinstance(error, dict):
            self.failure = str(error.get("message") or "").strip() or _short_json(error)
        elif error:
            self.failure = str(error)
        feedback = payload.get("promptFeedback")
        if isinstance(feedback, dict):
            reason = feedback.get("blockReason")
            if reason:
                self.block_reason = str(reason)
        usage = payload.get("usageMetadata")
        if isinstance(usage, dict):
            self.usage = usage
        candidates = payload.get("candidates")
        for candidate in candidates if isinstance(candidates, list) else []:
            if not isinstance(candidate, dict):
                continue
            finish_reason = candidate.get("finishReason")
            if finish_reason:
                self.finish_reason = str(finish_reason)
            content = candidate.get("content")
            parts = content.get("parts") if isinstance(content, dict) else None
            for part in parts if isinstance(parts, list) else []:
                if not isinstance(part, dict) or part.get("thought") is True:
                    continue
                text = part.get("text")
                if isinstance(text, str) and text:
                    self.text += strip_think_tags(text)

    def apply_event(self, event: Mapping[str, Any]) -> None:
        """Apply one decoded server-sent event, which may wrap the response payload."""
        payload = event.get("response")
        self.apply_payload(payload if isinstance(payload, dict) else event)

    def answer(self, provider: str) -> str:
        """Return the answer text, refusing failed, blocked, truncated or empty replies."""
        if self.failure is not None:
            raise RuntimeError(f"{provider} response failed: {self.failure}")
        if self.finish_reason in TRUNCATED_REASONS:
            raise TruncatedResponseError(f"{provider} response was truncated at the token limit")
        if not self.text.strip():
            reason = self.block_reason or self.finish_reason
            if reason is not None and (
                self.block_reason is not None or str(reason) in BLOCKED_REASONS
            ):
                raise RuntimeError(f"{provider} refused the request (reason={reason})")
            raise EmptyResponseError(f"{provider} response content is empty")
        return self.text


class CloudCodeClient(ProfileAdapter[ProviderOptions]):
    """Post one Cloud Code envelope and read its event stream."""

    def available_models(self, credential: OAuthCredential) -> tuple[str, ...]:
        """Fetch the current Cloud Code model catalog with the authorized project."""
        headers = {
            **self.profile.default_headers,
            **self.profile.auth_headers,
            "authorization": f"Bearer {credential.access_token}",
        }
        try:
            with self.http_client() as client:
                response = client.post(
                    f"{self.resolve_base_url()}/v1internal:fetchAvailableModels",
                    json={"project": credential.project_id or DEFAULT_PROJECT},
                    headers=headers,
                )
        except httpx.HTTPError:
            raise ValueError(
                "Cannot list Antigravity models; check your network and retry"
            ) from None
        if not 200 <= response.status_code < 300:
            raise ValueError(f"Cannot list Antigravity models: HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            raise ValueError("Cannot list Antigravity models: invalid JSON response") from None
        models = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(models, dict):
            raise ValueError("Cannot list Antigravity models: invalid catalog response")
        return tuple(key for key in models if isinstance(key, str) and key.strip())

    def build_body(
        self,
        messages: Messages,
        model: ResolvedModel[ProviderOptions],
        *,
        json_mode: bool,
        context: RequestContext,
    ) -> dict[str, Any]:
        """Build the Cloud Code envelope for one attempt."""
        options = model.options
        inner = build_request(messages)
        extra_body, top_level = self.reasoning_parts(model.model, options)
        merge_extra_body(inner, top_level, extra_body, options.extra_body)
        config = generation_config(options, max_tokens=context.max_tokens, json_mode=json_mode)
        if config:
            inner["generationConfig"] = config
        return {
            "project": self.credential().project_id or DEFAULT_PROJECT,
            "model": model.model,
            "userAgent": USER_AGENT_IDENTITY,
            "requestType": REQUEST_TYPE,
            "requestId": request_id(),
            "enabledCreditTypes": [CREDIT_TYPE],
            "request": inner,
        }

    def read_outcome(self, body: dict[str, Any]) -> CloudCodeOutcome:
        """Send one request and accumulate its event stream or its JSON response."""
        outcome = CloudCodeOutcome()
        with self.open_stream(STREAM_PATH, body) as response:
            if is_event_stream(response):
                for event in iter_sse_json(response.iter_lines()):
                    outcome.apply_event(event)
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
        record(context, gemini_style_usage(outcome.usage))
        return outcome.answer(self.profile.display())


__all__ = [
    "BLOCKED_REASONS",
    "CREDIT_TYPE",
    "DEFAULT_PROJECT",
    "STREAM_PATH",
    "TRUNCATED_REASONS",
    "CloudCodeClient",
    "CloudCodeOutcome",
    "build_request",
    "generation_config",
    "request_id",
    "strip_think_tags",
]
