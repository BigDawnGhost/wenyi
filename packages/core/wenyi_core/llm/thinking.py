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
