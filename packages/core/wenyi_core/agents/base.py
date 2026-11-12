"""Shared agent initialization and LLM helpers with optional fallback values.
Centralize the render-system/user, complete_json and fallback pattern. Business fallback
semantics belong here, not in the LLM transport layer. Every agent exposes .src for language
propagation by the pipeline.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from ..config import Config
from ..i18n.policy.models import Phase
from ..i18n.prompts import render
from ..llm.base import LLMClient
from ..llm.json_parser import parse_json_loose

_RAISE = object()  # Sentinel: propagate exceptions when the caller supplies no default.
_LOGGER = logging.getLogger(__name__)

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
    ) -> tuple[Any, str]:
        """Run ``complete`` on ``messages`` and return ``(parsed_json, raw_assistant_text)``."""
        text = self.client.complete(
            messages,
            operation=operation,
            json_mode=True,
            max_tokens=max_tokens,
        )
        return parse_json_loose(text), text

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
        """Send system/user messages through complete_json.
        Return default on failure, or propagate when no default is supplied (for example,
        Translator handles alignment retries). With key, use data[key] for dictionaries, a
        nonempty list directly, or the fallback otherwise. With item_type, a keyed list must
        contain only items of that type or the fallback is returned instead.
        """
        try:
            data, _raw = self._complete_json_turn(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                operation=operation,
                max_tokens=max_tokens,
            )
        except Exception as error:
            if default is _RAISE:
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
        """Like ``_ask_json`` but for an already-built message list (multi-turn continues)."""
        try:
            data, _raw = self._complete_json_turn(
                messages,
                operation=operation,
                max_tokens=max_tokens,
            )
        except Exception as error:
            if default is _RAISE:
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
        """Complete plain text and strip whitespace; return default on failure."""
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
