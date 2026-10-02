"""Anthropic Messages wire protocol for profile-driven providers.

The profile supplies the endpoint, the ``x-api-key`` credential and the
``anthropic-version`` header. This adapter owns the Messages request shape: system prompts
move to a top-level ``system`` field, consecutive same-role turns are merged, ``max_tokens``
is mandatory, and extended thinking is a token budget that must stay below the output cap.
"""

from __future__ import annotations

from typing import Any

from ...retrying import EmptyResponseError, TruncatedResponseError
from ...transport import Messages, RequestContext, ResolvedModel
from ...usage import UsageSample, make_usage_sample, read_usage_int
from .._oauth import record
from .._openai_compatible import json_instruction_messages
from .._options import ProviderOptions
from .base import ProfileAdapter, merge_extra_body

MESSAGES_PATH = "/v1/messages"
DEFAULT_MAX_TOKENS = 4096
MIN_THINKING_BUDGET = 1024
# Extended thinking needs the minimum budget plus headroom for the answer itself.
MIN_THINKING_OUTPUT = 2 * MIN_THINKING_BUDGET
TRUNCATION_REASONS = frozenset({"max_tokens"})


def thinking_budget(max_tokens: int) -> int | None:
    """Return the reasoning budget that fits under ``max_tokens``, or None when it cannot."""
    if max_tokens < MIN_THINKING_OUTPUT:
        return None
    return max(MIN_THINKING_BUDGET, max_tokens // 2)


def split_messages(messages: Messages) -> tuple[str | None, list[dict[str, Any]]]:
    """Move system prompts into ``system`` and merge consecutive same-role turns.

    The Messages API accepts only user and assistant turns, requires them to alternate, and
    carries the system prompt outside the conversation.
    """
    instructions: list[str] = []
    turns: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role", "user"))
        content = str(message.get("content", "") or "")
        if role == "system":
            if content.strip():
                instructions.append(content)
            continue
        turn_role = "assistant" if role == "assistant" else "user"
        if turns and turns[-1]["role"] == turn_role:
            turns[-1]["content"] = f"{turns[-1]['content']}\n\n{content}"
        else:
            turns.append({"role": turn_role, "content": content})
    if not turns:
        turns.append({"role": "user", "content": ""})
    return ("\n\n".join(instructions) or None), turns


def completion_text(payload: dict[str, Any], provider: str) -> str:
    """Read the answer from the text blocks, refusing truncated or empty completions."""
    if str(payload.get("stop_reason") or "").lower() in TRUNCATION_REASONS:
        raise TruncatedResponseError(f"{provider} response was truncated at the token limit")
    content = payload.get("content")
    blocks = content if isinstance(content, list) else []
    text = "".join(
        str(block.get("text") or "")
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "text"
    )
    if not text.strip():
        raise EmptyResponseError(f"{provider} response content is empty")
    return text


def anthropic_usage(payload: dict[str, Any]) -> UsageSample | None:
    """Normalize Anthropic token counters, mapping cache reads to cache hits."""
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    prompt_tokens = read_usage_int(usage, "input_tokens")
    completion_tokens = read_usage_int(usage, "output_tokens")
    cache_hit_tokens = read_usage_int(usage, "cache_read_input_tokens")
    cache_creation_tokens = read_usage_int(usage, "cache_creation_input_tokens")
    prompt_tokens += cache_hit_tokens + cache_creation_tokens
    return make_usage_sample(
        {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
        cache_hit_tokens=cache_hit_tokens,
        cache_miss_tokens=max(0, prompt_tokens - cache_hit_tokens),
    )


class AnthropicClient(ProfileAdapter[ProviderOptions]):
    """Post one Messages body using the profile's headers and endpoint."""

    def request_headers(self, *, accept: str = "application/json") -> dict[str, str]:
        headers = super().request_headers(accept=accept)
        headers.setdefault("anthropic-version", "2023-06-01")
        return headers

    def _request(
        self,
        messages: Messages,
        model: ResolvedModel[ProviderOptions],
        *,
        json_mode: bool,
        context: RequestContext,
    ) -> str:
        options = model.options
        max_tokens = context.max_tokens or DEFAULT_MAX_TOKENS
        prepared = json_instruction_messages(messages) if json_mode else messages
        system, turns = split_messages(prepared)
        body: dict[str, Any] = {
            "model": model.model,
            "max_tokens": max_tokens,
            "messages": turns,
            "stream": False,
        }
        if system is not None:
            body["system"] = system
        extra_body, top_level = self.reasoning_parts(model.model, options)
        merge_extra_body(body, top_level)
        if self.profile.reasoning is None and options.thinking_enabled(
            self.profile.default_thinking
        ):
            budget = thinking_budget(max_tokens)
            if budget is not None:
                body["thinking"] = {"type": "enabled", "budget_tokens": budget}
        # Extended thinking fixes sampling, so a temperature is only sent without it.
        if options.temperature is not None and "thinking" not in body:
            body["temperature"] = options.temperature
        merge_extra_body(body, extra_body, options.extra_body)
        path = "/messages" if self.resolve_base_url().endswith("/v1") else MESSAGES_PATH
        payload = self.post_json(path, body)
        record(context, anthropic_usage(payload))
        return completion_text(payload, self.profile.display())


__all__ = [
    "AnthropicClient",
    "DEFAULT_MAX_TOKENS",
    "MESSAGES_PATH",
    "MIN_THINKING_BUDGET",
    "MIN_THINKING_OUTPUT",
    "anthropic_usage",
    "completion_text",
    "split_messages",
    "thinking_budget",
]
