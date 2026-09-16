"""Tolerant parsing of model JSON output."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from json_repair import repair_json


class JsonParseError(ValueError):
    """Model output remains unusable JSON after local repair."""


@dataclass(frozen=True)
class JsonParseResult:
    """Parsed JSON result and whether syntax repair was needed."""

    value: Any
    repaired: bool


def parse_json_result(text: str) -> JsonParseResult:
    """Parse model JSON and accurately record whether syntax repair occurred.
    Run json.loads once to determine the repaired flag. On failure, call json-repair with
    skip_json_loads=True to avoid repeating strict parsing.
    """
    raw = (text or "").strip()
    try:
        return JsonParseResult(json.loads(raw), repaired=False)
    except (json.JSONDecodeError, TypeError):
        pass

    try:
        value = repair_json(
            raw,
            return_objects=True,
            skip_json_loads=True,
        )
    except Exception as error:
        raise JsonParseError(f"Cannot parse JSON: {raw[:200]!r}") from error
    # json-repair returns an empty string for empty/plain-language input; that is not recoverable JSON.
    if value == "":
        raise JsonParseError(f"Cannot parse JSON: {raw[:200]!r}")
    return JsonParseResult(value, repaired=True)


def parse_json_loose(text: str) -> Any:
    """Return the parsed value, delegating syntax recovery to json-repair."""
    return parse_json_result(text).value
