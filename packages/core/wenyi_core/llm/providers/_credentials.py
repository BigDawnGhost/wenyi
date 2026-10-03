"""Credential resolution shared by every profile-driven wire adapter.

A credential reaches Wenyi through one environment variable: either a plain API key or a JSON
OAuth credential. Subscription credentials are refreshed in memory when they are close to
expiry and never written back, so a long run keeps working without touching the user's shell.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import replace
from typing import Any

import httpx

from .. import subscriptions
from ..oauth.credentials import (
    OAuthCredential,
    OAuthCredentialError,
)
from ..oauth.importers import parse_credential_text
from ..profiles import (
    AUTH_API_KEY,
    AUTH_COPILOT,
    AUTH_OAUTH_DEVICE,
    AUTH_OAUTH_IMPORT,
    AUTH_OAUTH_PKCE,
    AUTH_OAUTH_USER_CODE,
    AUTH_VERTEX,
    ProviderProfile,
)

COPILOT_TOKEN_EXCHANGE_URL = subscriptions.COPILOT_TOKEN_EXCHANGE_URL
COPILOT_EDITOR_HEADERS = {
    "editor-version": "wenyi/1.0",
    "editor-plugin-version": "wenyi/1.0",
    "copilot-integration-id": "vscode-chat",
    "user-agent": "GitHubCopilotChat/0.26.7",
}
VERTEX_DEFAULT_REGION = "global"


def candidate_env_vars(profile: ProviderProfile, api_key_env: str | None = None) -> tuple[str, ...]:
    """Return the ordered credential environment variables for a connection."""
    names: list[str] = []
    if api_key_env:
        names.append(api_key_env)
    subscription = subscriptions.SUBSCRIPTIONS_BY_KIND.get(profile.kind)
    if subscription is not None:
        names.append(subscription.env_var)
    names.extend(profile.api_key_envs)
    seen: set[str] = set()
    ordered: list[str] = []
    for name in names:
        if name and name not in seen:
            seen.add(name)
            ordered.append(name)
    return tuple(ordered)


def read_env_value(names: tuple[str, ...]) -> tuple[str, str | None]:
    """Return the first non-empty value and the variable that supplied it."""
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value, name
    return "", None


def configured_base_url(profile: ProviderProfile, base_url: str | None) -> str | None:
    """Resolve the endpoint from the connection, then its profile, then the environment."""
    if base_url:
        return base_url
    if profile.base_url_env:
        override = os.environ.get(profile.base_url_env, "").strip()
        if override:
            return override
    return profile.base_url


def parse_credential(value: str) -> OAuthCredential:
    """Parse an environment value as a JSON OAuth credential, else as a plain token."""
    parsed = parse_credential_text(value)
    if parsed:
        return parsed[0]
    return OAuthCredential(access_token=value)


def resolve_credential(
    profile: ProviderProfile,
    *,
    api_key_env: str | None = None,
    cached: OAuthCredential | None = None,
) -> OAuthCredential:
    """Resolve a credential, preserving rotated refresh tokens held by the connection."""
    names = candidate_env_vars(profile, api_key_env)
    value, env_name = read_env_value(names)
    if not value:
        if not profile.requires_api_key and profile.auth == AUTH_API_KEY:
            return OAuthCredential(access_token="no-key")
        expected = names[0] if names else f"WENYI_{profile.kind.upper()}_OAUTH"
        raise OAuthCredentialError(
            f"Environment variable {expected} is not set for provider {profile.kind}; "
            f"run `wenyi auth login {profile.kind}` or export an API key"
        )
    credential = cached if cached is not None else parse_credential(value)
    if profile.auth == AUTH_COPILOT:
        return _copilot_credential(credential)
    if not credential.refresh_token and not _looks_like_subscription(profile):
        return credential
    if not credential.expired():
        return credential
    refreshed = _refresh(profile, credential, env_name)
    if not refreshed.access_token:
        raise OAuthCredentialError(
            f"The {profile.display()} credential in {env_name} has no access token; "
            f"run `wenyi auth login {profile.kind}` again"
        )
    return refreshed


def resolve_access_token(
    profile: ProviderProfile,
    *,
    api_key_env: str | None = None,
) -> str:
    """Return a usable bearer token, refreshing an expired subscription credential once."""
    return resolve_credential(profile, api_key_env=api_key_env).access_token


def _looks_like_subscription(profile: ProviderProfile) -> bool:
    """Report whether the provider is expected to carry refreshable credentials."""
    return profile.auth in {
        AUTH_OAUTH_DEVICE,
        AUTH_OAUTH_PKCE,
        AUTH_OAUTH_IMPORT,
        AUTH_OAUTH_USER_CODE,
    } and bool(profile.api_key_envs[:1])


def _refresh(
    profile: ProviderProfile, credential: OAuthCredential, env_name: str | None
) -> OAuthCredential:
    subscription = subscriptions.SUBSCRIPTIONS_BY_KIND.get(profile.kind)
    if subscription is None and env_name:
        subscription = subscriptions.subscription_for_env(env_name)
    if subscription is None or subscription.refresh is None:
        if credential.access_token and not credential.expired():
            return credential
        raise OAuthCredentialError(
            f"The {profile.display()} credential has expired and cannot be refreshed; "
            f"run `wenyi auth login {profile.kind}` again"
        )
    if not credential.refreshable():
        raise OAuthCredentialError(
            f"The {profile.display()} credential has no refresh token; "
            f"run `wenyi auth login {profile.kind}` again"
        )
    return subscription.refresh(credential)


def _copilot_credential(credential: OAuthCredential) -> OAuthCredential:
    """Return a valid Copilot credential, exchanging the stored GitHub token when needed."""
    if credential.expired() or credential.plan != "copilot":
        return exchange_copilot_token(credential)
    return credential


def exchange_copilot_token(credential: OAuthCredential) -> OAuthCredential:
    """Exchange a GitHub token for a short-lived Copilot API token."""
    github_token = (credential.refresh_token or credential.access_token).strip()
    if not github_token:
        raise OAuthCredentialError(
            "The GitHub Copilot credential has no GitHub token; run `wenyi auth login copilot`"
        )
    with httpx.Client(timeout=30.0) as client:
        response = client.get(
            COPILOT_TOKEN_EXCHANGE_URL,
            headers={
                "authorization": f"token {github_token}",
                "accept": "application/json",
                **COPILOT_EDITOR_HEADERS,
            },
        )
    if response.status_code in {401, 403}:
        raise OAuthCredentialError(
            "GitHub rejected the stored token for Copilot; run `wenyi auth login copilot` again"
        )
    if not 200 <= response.status_code < 300:
        raise OAuthCredentialError(f"Copilot token exchange failed (HTTP {response.status_code})")
    payload = _json_object(response)
    token = str(payload.get("token") or "").strip()
    if not token:
        raise OAuthCredentialError("Copilot token exchange returned no token")
    endpoints = payload.get("endpoints")
    account_endpoint = (
        str(endpoints.get("api") or "").strip() if isinstance(endpoints, dict) else ""
    )
    if not account_endpoint:
        match = re.search(r"(?:^|;)\s*proxy-ep=([^;\s]+)", token)
        if match:
            host = re.sub(r"^https?://", "", match.group(1)).rstrip("/")
            host = re.sub(r"^proxy\.", "api.", host)
            account_endpoint = f"https://{host}"
    return replace(
        credential,
        api_base_url=account_endpoint or None,
        access_token=token,
        refresh_token=github_token,
        expires_at=_copilot_expiry(payload),
        plan="copilot",
    )


def _copilot_expiry(payload: dict[str, Any]) -> float:
    raw = payload.get("expires_at")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return time.time() + 600.0
    return value / 1000 if value > 10_000_000_000 else value


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def vertex_token_and_base_url(profile: ProviderProfile, base_url: str | None) -> tuple[str, str]:
    """Mint a Vertex AI access token and the OpenAI-compatible endpoint for the project."""
    region = (
        os.environ.get("VERTEX_REGION", "").strip()
        or os.environ.get("GOOGLE_CLOUD_REGION", "").strip()
        or VERTEX_DEFAULT_REGION
    )
    try:
        import google.auth
        from google.auth.transport.requests import Request
    except ImportError as error:  # pragma: no cover - dependency is installed with the package
        raise OAuthCredentialError(
            "Vertex AI requires the google-auth package: uv add google-auth"
        ) from error
    try:
        credentials_path = os.environ.get("VERTEX_CREDENTIALS_PATH", "").strip()
        scopes = ["https://www.googleapis.com/auth/cloud-platform"]
        if credentials_path:
            credentials, project = google.auth.load_credentials_from_file(
                credentials_path, scopes=scopes
            )
        else:
            credentials, project = google.auth.default(scopes=scopes)
        credentials.refresh(Request())
    except Exception as error:  # noqa: BLE001 - surface one actionable message
        raise OAuthCredentialError(
            "Cannot obtain Vertex AI credentials from the environment; set "
            "GOOGLE_APPLICATION_CREDENTIALS or VERTEX_CREDENTIALS_PATH, or run "
            "`gcloud auth application-default login`"
        ) from error
    resolved_project = (
        os.environ.get("VERTEX_PROJECT_ID", "").strip()
        or os.environ.get("VERTEX_PROJECT", "").strip()
        or os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
        or str(project or "").strip()
    )
    if not resolved_project:
        raise OAuthCredentialError(
            "Vertex AI needs a project ID; set VERTEX_PROJECT_ID or GOOGLE_CLOUD_PROJECT"
        )
    token = str(getattr(credentials, "token", "") or "")
    if not token:
        raise OAuthCredentialError("Vertex AI credential refresh returned no access token")
    host = (
        "aiplatform.googleapis.com" if region == "global" else f"{region}-aiplatform.googleapis.com"
    )
    endpoint = configured_base_url(profile, base_url)
    if not base_url and endpoint == profile.base_url:
        endpoint = f"https://{host}/v1beta1/projects/{resolved_project}/locations/{region}/endpoints/openapi"
    return token, endpoint


def api_key_candidates(profile: ProviderProfile, api_key_env: str | None) -> tuple[str, ...]:
    """Expose the ordered credential variables for validation messages."""
    return candidate_env_vars(profile, api_key_env)


def is_subscription_auth(profile: ProviderProfile) -> bool:
    """Report whether the provider is expected to authenticate through a subscription."""
    return profile.auth != AUTH_API_KEY and profile.auth != AUTH_VERTEX


__all__ = [
    "COPILOT_EDITOR_HEADERS",
    "api_key_candidates",
    "candidate_env_vars",
    "configured_base_url",
    "exchange_copilot_token",
    "is_subscription_auth",
    "parse_credential",
    "read_env_value",
    "resolve_access_token",
    "resolve_credential",
    "vertex_token_and_base_url",
]
