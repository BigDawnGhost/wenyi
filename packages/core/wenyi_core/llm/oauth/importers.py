"""Parse subscription credentials from existing JSON, text and environment files.

The shapes accepted here mirror the credential files written by the corresponding first
party clients, so an existing Codex, Grok CLI or Antigravity credential can be imported
without a new sign-in.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .credentials import (
    OAuthCredential,
    email_from_id_token,
    optional_number,
    optional_text,
)

ACCESS_KEYS = (
    "access_token",
    "accessToken",
    "access",
    "token",
    "apiKey",
    "api_key",
    "key",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "ANTIGRAVITY_API_KEY",
    "OPENAI_API_KEY",
    "XAI_API_KEY",
)
REFRESH_KEYS = ("refresh_token", "refreshToken", "refresh")
ACCOUNT_KEYS = ("account_id", "accountId", "chatgpt_account_id", "chatgptAccountId")
PROJECT_KEYS = ("project", "projectId", "project_id", "cloudaicompanionProject")
IDENTITY_KEYS = ("email", "display_name", "displayName", "preferred_username", "name")
PLAN_KEYS = ("plan", "plan_type", "planType", "subscriptionTier", "subscriptionTierDisplay")
EXPIRY_KEYS = ("expires_at", "expiresAt", "expiry", "expiry_date")
EXPIRY_IN_KEYS = ("expires_in", "expiresIn")
ID_TOKEN_KEYS = ("id_token", "idToken")
NESTED_KEYS = ("accounts", "credentials", "items", "keys")

_API_KEY_PATTERN = re.compile(r"AIza[0-9A-Za-z_-]{35}")
_ASSIGNMENT_PATTERN = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+?)\s*$")
_JWT_PATTERN = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*$")


def parse_credential_text(text: str) -> list[OAuthCredential]:
    """Parse one credential source: JSON, a bare token, or environment assignments."""
    raw = text.strip()
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except ValueError:
        return _parse_text_credentials(raw)
    return extract_credentials(payload)


def extract_credentials(payload: Any) -> list[OAuthCredential]:
    """Extract every credential contained in a decoded JSON payload."""
    found: list[OAuthCredential] = []
    _collect(payload, found)
    return found


def read_credential_files(
    paths: Sequence[Path],
) -> tuple[list[OAuthCredential], list[str]]:
    """Read credential files, returning the credentials found and per-file warnings."""
    credentials: list[OAuthCredential] = []
    warnings: list[str] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as error:
            warnings.append(f"{path.name}: {error.strerror or error}")
            continue
        found = parse_credential_text(text)
        if not found:
            warnings.append(f"{path.name}: no access token found")
            continue
        credentials.extend(found)
    return credentials, warnings


def _collect(value: Any, found: list[OAuthCredential]) -> None:
    if isinstance(value, str):
        token = value.strip()
        if token and not _looks_like_json(token):
            found.append(OAuthCredential(access_token=token, email=email_from_id_token(token)))
        return
    if isinstance(value, list):
        for item in value:
            _collect(item, found)
        return
    if not isinstance(value, Mapping):
        return
    if value.get("disabled") is True:
        return
    for key in NESTED_KEYS:
        nested = value.get(key)
        if isinstance(nested, list):
            for item in nested:
                _collect(item, found)
            return
    tokens = value.get("tokens")
    scope: Mapping[str, Any] = tokens if isinstance(tokens, Mapping) else value
    credential = _credential_from_mapping(value, scope)
    if credential is not None:
        found.append(credential)


def _credential_from_mapping(
    outer: Mapping[str, Any], scope: Mapping[str, Any]
) -> OAuthCredential | None:
    access_token = _first(scope, ACCESS_KEYS) or _first(outer, ACCESS_KEYS) or ""
    refresh_token = _first(scope, REFRESH_KEYS) or _first(outer, REFRESH_KEYS)
    if not access_token and not refresh_token:
        return None
    id_token = _first(scope, ID_TOKEN_KEYS) or _first(outer, ID_TOKEN_KEYS)
    email = (
        _first(outer, IDENTITY_KEYS)
        or _first(scope, IDENTITY_KEYS)
        or email_from_id_token(id_token)
        or email_from_id_token(access_token)
    )
    return OAuthCredential(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=_expiry(scope, outer),
        account_id=_first(scope, ACCOUNT_KEYS) or _first(outer, ACCOUNT_KEYS),
        project_id=_first(scope, PROJECT_KEYS) or _first(outer, PROJECT_KEYS),
        email=email,
        plan=_first(scope, PLAN_KEYS) or _first(outer, PLAN_KEYS),
        api_base_url=_first(scope, ("api_base_url",)) or _first(outer, ("api_base_url",)),
    )


def _expiry(scope: Mapping[str, Any], outer: Mapping[str, Any]) -> float | None:
    for source in (scope, outer):
        for key in EXPIRY_KEYS:
            if key in source:
                parsed = _timestamp(source[key])
                if parsed is not None:
                    return parsed
        for key in EXPIRY_IN_KEYS:
            seconds = optional_number(source.get(key))
            if seconds is not None:
                return time.time() + seconds
    return None


def _timestamp(value: Any) -> float | None:
    numeric = optional_number(value)
    if numeric is not None:
        # Normalize millisecond timestamps to seconds.
        return numeric / 1000.0 if numeric > 10_000_000_000 else numeric
    text = optional_text(value)
    if not text:
        return None
    try:
        from datetime import datetime, timezone

        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _first(source: Mapping[str, Any], keys: Sequence[str]) -> str | None:
    for key in keys:
        value = optional_text(source.get(key))
        if value:
            return value
    normalized = {str(key).lower().replace("_", ""): key for key in source}
    for key in keys:
        match = normalized.get(key.lower().replace("_", ""))
        if match is None:
            continue
        value = optional_text(source.get(match))
        if value:
            return value
    return None


def _parse_text_credentials(raw: str) -> list[OAuthCredential]:
    assignments: dict[str, str] = {}
    for line in raw.splitlines():
        match = _ASSIGNMENT_PATTERN.match(line)
        if match is None:
            continue
        name, value = match.group(1), match.group(2).strip().strip("'\"")
        if value:
            assignments[name] = value
    credential = _credential_from_mapping(assignments, assignments)
    if credential is not None:
        return [credential]

    # Fall back to scanning for a bare API key or JSON Web Token.
    found: list[OAuthCredential] = []
    api_key = _API_KEY_PATTERN.search(raw)
    if api_key is not None:
        found.append(OAuthCredential(access_token=api_key.group(0)))
    elif _JWT_PATTERN.match(raw.strip()):
        token = raw.strip()
        found.append(OAuthCredential(access_token=token, email=email_from_id_token(token)))
    return found


def _looks_like_json(text: str) -> bool:
    stripped = text.lstrip()
    return stripped[:1] in {"{", "["}
