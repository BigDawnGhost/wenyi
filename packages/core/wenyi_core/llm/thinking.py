"""Provider-declared controls for a route's primary model."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ThinkingControl:
    levels: tuple[str, ...]
    update: Callable[[str], dict[str, Any]]
    read: Callable[[dict[str, Any]], str | None]
    raw_field: str
    raw_keys: tuple[str, ...]

    def choices(self, options: dict[str, Any]) -> tuple[str, ...]:
        # Raw provider settings can take precedence on the wire. Do not offer an
        # override that might silently be ignored.
        raw = options.get(self.raw_field, {})
        if any(key in raw for key in self.raw_keys):
            return ()
        return self.levels

    def apply(self, options: dict[str, Any], level: str) -> dict[str, Any]:
        if level not in self.choices(options):
            raise ValueError(
                f"Thinking level {level!r} is not supported by this model configuration"
            )
        return {**options, **self.update(level)}

    def escalate(
        self,
        resolved_options: dict[str, Any],
        *,
        steps: int = 1,
        max_auto_level: str = "high",
    ) -> str | None:
        """Step up the provider's thinking ladder without overriding ``off`` or raw configs.

        Returns the escalated level name, or ``None`` when escalation is unavailable or the
        resolved route is already at or above the automatic escalation ceiling.
        """
        if steps <= 0:
            return None
        choices = self.choices(resolved_options)
        if not choices:
            return None
        current = self.read(resolved_options)
        if current is None or current == "off" or current not in choices:
            return None
        ladder = tuple(level for level in choices if level != "off")
        if current not in ladder:
            return None
        if max_auto_level in ladder:
            ceiling_idx = ladder.index(max_auto_level)
        else:
            ceiling_idx = len(ladder) - 1
        current_idx = ladder.index(current)
        if current_idx >= ceiling_idx:
            return None
        target_idx = min(current_idx + steps, ceiling_idx)
        return ladder[target_idx] if target_idx > current_idx else None


def effort_control(*levels: str, raw_field: str = "extra_body") -> ThinkingControl:
    def update(level: str) -> dict[str, Any]:
        return (
            {"thinking": False} if level == "off" else {"thinking": True, "reasoning_effort": level}
        )

    def read(options: dict[str, Any]) -> str | None:
        if not options["thinking"]:
            return "off" if "off" in levels else None
        return options["reasoning_effort"]

    return ThinkingControl(
        levels, update, read, raw_field, ("thinking", "reasoning", "reasoning_effort")
    )
