"""Normalize model-supplied metadata fields using the current English schema."""


def normalize_term_type(value: str) -> str:
    """Normalize known types without discarding custom categories."""
    value = value.strip()
    return value or "term"


def normalize_gender(value: str) -> str:
    """Use English values and preserve an empty value for unknown gender."""
    value = value.strip()
    return "" if value == "unknown" else value
