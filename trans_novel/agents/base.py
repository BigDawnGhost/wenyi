"""Shared agent initialization and LLM helpers with optional fallback values.
Centralize the render-system/user, complete_json and fallback pattern. Business fallback
semantics belong here, not in the LLM transport layer. Every agent exposes .src for language
propagation by the pipeline.
"""

from __future__ import annotations

from typing import Any

from ..config import Config
from ..llm.base import LLMClient

_RAISE = object()  # Sentinel: propagate exceptions when the caller supplies no default.


class Agent:
    def __init__(self, client: LLMClient, config: Config):
        """Store the shared client and config and cache the current source and target
        languages.
        """
        self.client = client
        self.config = config
        self.src = config.source_lang
        self.tgt = config.target_lang

    def _ask_json(
        self,
        system: str,
        user: str,
        *,
        operation: str,
        key: str | None = None,
        default: Any = _RAISE,
        max_tokens: int | None = None,
    ) -> Any:
        """Send system/user messages through complete_json.
        Return default on failure, or propagate when no default is supplied (for example,
        Translator handles alignment retries). With key, use data[key] for dictionaries, a
        nonempty list directly, or the fallback otherwise.
        """
        try:
            data = self.client.complete_json(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                operation=operation,
                max_tokens=max_tokens,
            )
        except Exception:
            if default is _RAISE:
                raise
            return default
        if key is None:
            return data
        fb = None if default is _RAISE else default
        if isinstance(data, dict):
            return data.get(key, fb)
        return data if data else fb

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
        except Exception:  # noqa: BLE001 - Text helper calls return the configured fallback on failure.
            return default

    @staticmethod
    def dict_items(items: Any) -> list[dict]:
        """Keep dictionary items from model collections such as issues and terms."""
        return [i for i in items or [] if isinstance(i, dict)]
