"""HMAC signatures binding a queue task name to its exact payload.

Producers sign with the deployment's shared secret (the API token); worker
entries verify before executing, so unsigned or tampered queue payloads are
rejected instead of run.
"""

from __future__ import annotations

import hashlib
import hmac
import json

SIGNATURE_FIELD = "payload_signature"


def payload_from(kwargs: dict) -> dict:
    """Task payload as the worker will see it, minus queue-reserved fields."""
    return {
        key: value
        for key, value in kwargs.items()
        if not key.startswith("_") and key != SIGNATURE_FIELD
    }


def sign_task(name: str, kwargs: dict, secret: str | None) -> str:
    """Sign the canonical JSON of the task name and its worker-visible payload."""
    message = json.dumps(
        {"name": name, "kwargs": payload_from(kwargs)},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    key = (secret or "").encode("utf-8")
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_task(name: str, kwargs: dict, signature: object, secret: str | None) -> bool:
    """Constant-time check of a payload signature against the shared secret."""
    if not isinstance(signature, str):
        return False
    expected = sign_task(name, kwargs, secret)
    return hmac.compare_digest(signature.encode("utf-8"), expected.encode("utf-8"))
