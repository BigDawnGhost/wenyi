"""Tolerant parsing of model JSON output."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from json_repair import repair_json


class JsonParseError(ValueError):
    """Model output remains unusable JSON after local repair."""


@dataclass(frozen=True)
class JsonParseResult:
    """Parsed JSON result with strict/repair and structural-safety metadata."""

    value: Any
    repaired: bool
    repair_kind: Literal["none", "boundary_only", "structural"]

    @property
    def safe_for_complete_payload(self) -> bool:
        """Whether parsing proved that repair did not synthesize payload content."""
        return self.repair_kind in {"none", "boundary_only"}


def _same_json_value(left: Any, right: Any) -> bool:
    """Compare JSON values without conflating booleans and integers."""
    left_json = json.dumps(left, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    right_json = json.dumps(
        right,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return left_json == right_json


_PARSE_FAILED = object()


def _strict_value(candidate: str) -> Any:
    try:
        return json.loads(candidate)
    except (json.JSONDecodeError, TypeError):
        return _PARSE_FAILED


def _is_boundary_only_repair(raw: str, repaired_value: Any) -> bool:
    """Recognize repairs that expose an unchanged strict JSON value.

    The accepted transformations are deliberately narrow: one missing root closing delimiter,
    surplus closing delimiters after a complete value, or a strict JSON value inside a Markdown
    JSON fence. Anything that requires completing a string, collection item, key or value remains
    structurally ambiguous.
    """
    for delimiter in ("}", "]"):
        candidate = _strict_value(raw + delimiter)
        if candidate is not _PARSE_FAILED and _same_json_value(candidate, repaired_value):
            return True

    without_extra = raw
    while without_extra.endswith(("}", "]")):
        without_extra = without_extra[:-1].rstrip()
        candidate = _strict_value(without_extra)
        if candidate is not _PARSE_FAILED and _same_json_value(candidate, repaired_value):
            return True

    lines = raw.splitlines()
    if (
        len(lines) >= 3
        and lines[0].strip().lower() in {"```", "```json"}
        and lines[-1].strip() == "```"
    ):
        candidate = _strict_value("\n".join(lines[1:-1]).strip())
        if candidate is not _PARSE_FAILED and _same_json_value(candidate, repaired_value):
            return True
    return False


def parse_json_result(text: str) -> JsonParseResult:
    """Parse model JSON and classify whether repair only changed an outer boundary.

    Run json.loads once to determine the repaired flag. On failure, call json-repair with
    skip_json_loads=True to avoid repeating strict parsing.
    """
    raw = (text or "").strip()
    try:
        return JsonParseResult(json.loads(raw), repaired=False, repair_kind="none")
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
    repair_kind = "boundary_only" if _is_boundary_only_repair(raw, value) else "structural"
    return JsonParseResult(value, repaired=True, repair_kind=repair_kind)


def parse_json_loose(text: str) -> Any:
    """Return the parsed value, delegating syntax recovery to json-repair."""
    return parse_json_result(text).value
