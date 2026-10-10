"""Metadata ownership rules for recoverable terminology publication."""

from __future__ import annotations

from typing import Any

from ..glossary.store import GlossaryTerm

_FIELDS = ("note", "reading", "gender")


def protected_metadata_fields(current: GlossaryTerm | None, record: dict[str, Any]) -> set[str]:
    """Retain manual edits, including edits made during an interrupted publication."""
    protected = set(record.get("protected_fields", []))
    if current is None:
        return protected
    baseline = record.get("publication", {})
    previous = record.get("previous") or {}
    for field in _FIELDS:
        value = getattr(current, field)
        known = [baseline[field]] if field in baseline else []
        if record.get("status") == "prepared" and field in previous:
            known.append(previous[field])
        edited = value not in known if known else bool(value)
        if edited:
            protected.add(field)
    return protected
