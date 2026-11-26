"""Shared agent initialization and LLM helpers with optional fallback values.
Business fallback semantics belong here, not in the LLM transport layer.
Every agent exposes .src for language propagation by the pipeline.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from ..config import Config
from ..errors import WenyiError
from ..i18n.policy.models import Phase
from ..i18n.prompts import render
from ..llm.base import LLMClient
from ..llm.json_parser import JsonParseError, UnsafeJsonRepairError, parse_json_result
from ..llm.retrying import EmptyResponseError, ProviderRequestError, TruncatedResponseError

_RAISE = object()  # Sentinel: propagate exceptions when the caller supplies no default.
_LOGGER = logging.getLogger(__name__)

# Failures an agent may absorb into its configured fallback: unusable model output and
# transient provider or transport problems. Budget stops and cancellations derive from
# BaseException and never reach these handlers; any other WenyiError or ValueError
# (configuration, routing, semantic validation) must propagate to the caller.
_FALLBACKABLE_ERRORS = (
    ProviderRequestError,
    EmptyResponseError,
    TruncatedResponseError,
    JsonParseError,
)


def _is_fallbackable(error: Exception) -> bool:
    """Return True when the error may be replaced by an agent's fallback value."""
    if isinstance(error, _FALLBACKABLE_ERRORS):
        return True
    return not isinstance(error, (WenyiError, ValueError))


Messages = list[dict[str, str]]


def _validated_collection(
    value: Any,
    item_type: type | None,
    operation: str,
    key: str | None,
    fallback: Any,
) -> Any:
    """Return ``value`` unchanged, or the fallback when a keyed list violates ``item_type``.

    Model output of the right shape but wrong element type (nulls, numbers, booleans inside a
    string list) must not be coerced into formal state; callers keep their configured fallback,
    matching the translator's strict per-item guard.
    """
    if item_type is None or not isinstance(value, list):
        return value
    if item_type is bool:
        items_ok = all(isinstance(item, bool) for item in value)
    else:
        items_ok = all(isinstance(item, item_type) and not isinstance(item, bool) for item in value)
    if items_ok:
        return value
    invalid = [type(item).__name__ for item in value if not isinstance(item, item_type)]
    _LOGGER.warning(
        "Ignoring model collection with invalid item types (operation=%s, key=%s): %s",
        operation,
        key or "<root>",
        ", ".join(sorted(set(invalid))) or "none",
    )
    return fallback


class Agent:
    policy_phase: ClassVar[Phase] = "translation"

    def __init__(self, client: LLMClient, config: Config):
        """Store the shared client and config and cache the current source and target
        languages.
        """
        self.client = client
        self.config = config
        self.src = config.source_lang
        self.tgt = config.target_lang
        self.language_policy = config.language_policy(self.policy_phase)

    def render(self, name: str, **kwargs: Any) -> str:
        """Use this agent's immutable resource view, including inside worker threads."""
        kwargs.setdefault("src", self.src)
        kwargs.setdefault("tgt", self.tgt)
        return render(name, plan=self.language_policy, **kwargs)

    def _complete_json_turn(
        self,
        messages: Messages,
        *,
        operation: str,
        max_tokens: int | None = None,
        require_complete_payload: bool = False,
    ) -> tuple[Any, str]:
        """Run ``complete`` on ``messages`` and return ``(parsed_json, raw_assistant_text)``.

        With ``require_complete_payload``, a reply that parses only after structural repair raises
        ``UnsafeJsonRepairError``: the repair may have folded stray text into payload values.
        """
        text = self.client.complete(
            messages,
            operation=operation,
            json_mode=True,
            max_tokens=max_tokens,
        )
        parsed = parse_json_result(text)
        if require_complete_payload and not parsed.safe_for_complete_payload:
            raise UnsafeJsonRepairError(f"JSON needed structural repair: {text[:200]!r}")
        return parsed.value, text

    def _ask_json(
        self,
        system: str,
        user: str,
        *,
        operation: str,
        key: str | None = None,
        default: Any = _RAISE,
        max_tokens: int | None = None,
        item_type: type | None = None,
    ) -> Any:
        """Send a system/user pair using the shared JSON request helper."""
        return self._ask_json_messages(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            operation=operation,
            key=key,
            default=default,
            max_tokens=max_tokens,
            item_type=item_type,
        )

    def _ask_json_messages(
        self,
        messages: Messages,
        *,
        operation: str,
        key: str | None = None,
        default: Any = _RAISE,
        max_tokens: int | None = None,
        item_type: type | None = None,
    ) -> Any:
        """Request JSON for a prepared conversation and return the parsed value.

        With ``key``, read that field from a JSON object or use a truthy non-object
        response directly. A missing field, falsy non-object, or list that fails
        ``item_type`` validation returns ``default`` (or ``None`` if omitted).
        Recoverable request or parse errors return ``default`` only if supplied;
        all other errors propagate.
        """
        try:
            data, _raw = self._complete_json_turn(
                messages,
                operation=operation,
                max_tokens=max_tokens,
            )
        except Exception as error:
            if default is _RAISE or not _is_fallbackable(error):
                raise
            _LOGGER.warning(
                "Agent call failed; returning fallback (operation=%s): %s: %s",
                operation,
                type(error).__name__,
                error,
            )
            return default
        if key is None:
            return data
        fb = None if default is _RAISE else default
        if isinstance(data, dict):
            return _validated_collection(data.get(key, fb), item_type, operation, key, fb)
        return _validated_collection(data if data else fb, item_type, operation, key, fb)

    def _ask_text(
        self,
        system: str,
        user: str,
        *,
        operation: str,
        default: str = "",
        max_tokens: int | None = None,
    ) -> str:
        """Complete plain text and strip whitespace; return default on recoverable failure."""
        try:
            return (
                self.client.complete(
                    [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    operation=operation,
                    max_tokens=max_tokens,
                )
                or ""
            ).strip()
        except Exception as error:  # noqa: BLE001 - Text helper calls return the configured fallback on failure.
            if not _is_fallbackable(error):
                raise
            _LOGGER.warning(
                "Agent call failed; returning fallback (operation=%s): %s: %s",
                operation,
                type(error).__name__,
                error,
            )
            return default

    @staticmethod
    def dict_items(
        items: Any, *, operation: str = "unknown", field: str = "collection"
    ) -> list[dict]:
        """Keep JSON objects, logging invalid types and counts without response content."""
        if items is None:
            return []
        if not isinstance(items, list):
            _LOGGER.warning(
                "Ignoring invalid model collection (operation=%s, field=%s): "
                "expected a JSON array, got %s",
                operation,
                field,
                type(items).__name__,
            )
            return []
        result = [item for item in items if isinstance(item, dict)]
        discarded = len(items) - len(result)
        if discarded:
            _LOGGER.warning(
                "Ignoring %d non-object items in model collection (operation=%s, field=%s)",
                discarded,
                operation,
                field,
            )
        return result
