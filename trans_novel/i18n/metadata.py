"""Canonical metadata values with read compatibility for earlier Chinese enums."""

from .resources import read_json


def normalize_term_type(value: str) -> str:
    """Normalize known types without discarding custom categories."""
    value = value.strip()
    return read_json("shared/metadata.json")["types"].get(value, value) or "term"


def normalize_gender(value: str) -> str:
    """Use English values and preserve an empty value for unknown gender."""
    value = value.strip()
    return read_json("shared/metadata.json")["genders"].get(value, value)
