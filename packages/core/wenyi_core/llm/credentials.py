"""Credential-scoped validation and diagnostic redaction, never model-content filtering."""

import unicodedata
from collections.abc import Iterable


def validate_credential(secret: str | None) -> None:
    """Reject control characters before any HTTP SDK can echo an invalid header."""
    if secret is not None and any(unicodedata.category(char).startswith("C") for char in secret):
        raise ValueError("API credentials cannot contain control characters; enter the key again")


class CredentialRedactor:
    """Redact the entire invocation snapshot, including other connections' credentials."""

    def __init__(self, credentials: Iterable[str | None]):
        self._secrets = sorted({value for value in credentials if value}, key=len, reverse=True)

    def __call__(self, value: str) -> str:
        for secret in self._secrets:
            value = value.replace(secret, "[redacted]")
        return value
