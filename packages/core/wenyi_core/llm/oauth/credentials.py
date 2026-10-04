"""Subscription OAuth credentials resolved from environment variables.

An initial OAuth credential lives in the environment variable named by the connection's
``api_key_env`` (or the provider default) as a JSON object. Wire adapters coordinate and
persist refresh-token replacements through the owner-only OAuth cache.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

import httpx

from ...envfile import export_lines

EXPIRY_SKEW_SECONDS = 300.0
TOKEN_REQUEST_TIMEOUT_SECONDS = 30.0


class OAuthCredentialError(RuntimeError):
    """A subscription credential is missing, malformed or no longer usable."""


@dataclass(frozen=True)
class OAuthCredential:
    """One subscription account; only the access token is mandatory."""

    access_token: str = ""
    refresh_token: str | None = None
    expires_at: float | None = None
    account_id: str | None = None
    project_id: str | None = None
    email: str | None = None
    plan: str | None = None
    api_base_url: str | None = None

    def expired(self, *, now: float | None = None, skew: float = EXPIRY_SKEW_SECONDS) -> bool:
        """Report whether the access token is absent or expires within ``skew`` seconds."""
        if not self.access_token:
            return True
        deadline = self.expires_at
        if deadline is None:
            deadline = jwt_expiry(self.access_token)
        if deadline is None:
            return False
        return deadline - skew <= (time.time() if now is None else now)

    def refreshable(self) -> bool:
        """Report whether a refresh token is available for coordinated refresh."""
        return bool(self.refresh_token)

    def with_token_response(self, payload: Mapping[str, Any]) -> OAuthCredential:
        """Apply an OAuth token response while preserving account identity fields."""
        access_token = optional_text(payload.get("access_token")) or self.access_token
        refresh_token = optional_text(payload.get("refresh_token")) or self.refresh_token
        expires_in = optional_number(payload.get("expires_in"))
        email = self.email or email_from_id_token(optional_text(payload.get("id_token")))
        return replace(
            self,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=None if expires_in is None else time.time() + expires_in,
            email=email,
        )

    def to_payload(self) -> dict[str, Any]:
        """Serialize to the JSON shape expected in the environment variable."""
        payload: dict[str, Any] = {"access_token": self.access_token}
        for key in ("refresh_token", "account_id", "project_id", "email", "plan", "api_base_url"):
            value = getattr(self, key)
            if value:
                payload[key] = value
        if self.expires_at is not None:
            payload["expires_at"] = round(self.expires_at, 3)
        return payload

    def describe(self) -> str:
        """Return a redacted one-line summary that is safe to print."""
        identity = self.email or self.account_id or self.project_id or "unknown account"
        details = [identity]
        if self.plan:
            details.append(self.plan)
        details.append(f"token …{fingerprint(self.access_token)}")
        if self.refresh_token:
            details.append("refreshable")
        return ", ".join(details)


def optional_text(value: Any) -> str | None:
    """Return a stripped string for non-empty string values."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def optional_number(value: Any) -> float | None:
    """Return a finite float for numeric values, including numeric strings."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value)
        except ValueError:
            return None
    return None


def decode_jwt_payload(token: str) -> dict[str, Any] | None:
    """Decode the JWT payload without verifying the signature; None when not a JWT."""
    parts = token.split(".")
    if len(parts) < 2:
        return None
    padded = parts[1].replace("-", "+").replace("_", "/")
    padded = padded + "=" * (-len(padded) % 4)
    try:
        decoded = base64.b64decode(padded)
        payload = json.loads(decoded)
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def jwt_expiry(token: str) -> float | None:
    """Read the ``exp`` claim of a JWT as epoch seconds."""
    payload = decode_jwt_payload(token)
    return None if payload is None else optional_number(payload.get("exp"))


def jwt_claim(token: str, *names: str) -> str | None:
    """Read the first present claim from a JWT payload."""
    payload = decode_jwt_payload(token)
    if payload is None:
        return None
    nested = payload.get("https://api.openai.com/profile")
    if isinstance(nested, dict):
        payload = {**nested, **payload}
    for name in names:
        value = optional_text(payload.get(name))
        if value:
            return value
    return None


def email_from_id_token(id_token: str | None) -> str | None:
    """Read the account email from an OIDC identity token."""
    if not id_token:
        return None
    return jwt_claim(id_token, "email", "preferred_username", "name")


def chatgpt_account_id(access_token: str) -> str | None:
    """Read the ChatGPT account identifier from a Codex access token."""
    payload = decode_jwt_payload(access_token)
    if payload is None:
        return None
    auth = payload.get("https://api.openai.com/auth")
    if isinstance(auth, dict):
        value = optional_text(auth.get("chatgpt_account_id"))
        if value:
            return value
    return optional_text(payload.get("chatgpt_account_id"))


def fingerprint(token: str, *, size: int = 6) -> str:
    """Return a short stable digest used to identify a token without revealing it."""
    import hashlib

    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:size]


def format_env_export(env_name: str, credential: OAuthCredential) -> str:
    """Return a shell assignment that stores the credential in the environment."""
    payload = json.dumps(credential.to_payload(), ensure_ascii=False, separators=(",", ":"))
    return export_lines({env_name: payload})


def load_credential(env_name: str, provider: str) -> OAuthCredential:
    """Load and validate the credential stored in ``env_name``."""
    from .importers import parse_credential_text

    raw = os.environ.get(env_name, "").strip()
    if not raw:
        raise OAuthCredentialError(
            f"Environment variable {env_name} is not set; run `wenyi auth login {provider}` "
            f"or `wenyi auth import {provider} <file>` and export the printed value"
        )
    credentials = parse_credential_text(raw)
    if not credentials:
        raise OAuthCredentialError(
            f"Environment variable {env_name} does not contain a usable access token"
        )
    return credentials[0]


def token_request(
    url: str,
    data: Mapping[str, str],
    *,
    client: httpx.Client | None = None,
    headers: Mapping[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Post an OAuth token request and return the status code with the parsed body."""
    request_headers = {
        "accept": "application/json",
        "content-type": "application/x-www-form-urlencoded",
        **(headers or {}),
    }
    if client is None:
        with httpx.Client(timeout=TOKEN_REQUEST_TIMEOUT_SECONDS) as owned:
            response = owned.post(url, data=dict(data), headers=request_headers)
    else:
        response = client.post(url, data=dict(data), headers=request_headers)
    return response.status_code, parse_json_object(response.text)


def parse_json_object(text: str) -> dict[str, Any]:
    """Parse a JSON object response, returning an empty mapping when it is not JSON."""
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def token_error_message(status: int, payload: Mapping[str, Any], text: str = "") -> str:
    """Build an actionable message from an OAuth error response."""
    detail = (
        optional_text(payload.get("error_description"))
        or optional_text(payload.get("error"))
        or optional_text(payload.get("message"))
        or text.strip()
        or f"HTTP {status}"
    )
    return f"OAuth token request failed (HTTP {status}): {detail}"


def refresh_access_token(
    url: str,
    data: Mapping[str, str],
    credential: OAuthCredential,
    *,
    client: httpx.Client | None = None,
) -> OAuthCredential:
    """Refresh a credential and return the replacement, without persisting it."""
    status, payload = token_request(url, data, client=client)
    if not 200 <= status < 300:
        raise OAuthCredentialError(token_error_message(status, payload))
    if not optional_text(payload.get("access_token")):
        raise OAuthCredentialError("OAuth refresh response did not include an access token")
    return credential.with_token_response(payload)
