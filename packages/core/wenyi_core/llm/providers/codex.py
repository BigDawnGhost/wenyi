"""ChatGPT subscription access through the official Codex Responses endpoint.

The endpoint speaks the Responses API, but it is streaming-only, rejects
``max_output_tokens``, and identifies the account through a claim of the access token rather
than through the request body. Those three vendor facts are the whole reason this adapter
exists next to the profile-driven Responses client.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from pydantic import BaseModel

from ..oauth.credentials import (
    OAuthCredential,
    chatgpt_account_id,
    jwt_claim,
)
from ..oauth.flows import DeviceCodeFlow
from .wire.responses import ResponsesClient

DEFAULT_API_KEY_ENV = "WENYI_CODEX_OAUTH"
MODELS_PATH = "/models?client_version=1.0.0"
ACCOUNT_HEADER = "ChatGPT-Account-Id"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
DEVICE_CODE_URL = "https://auth.openai.com/api/accounts/deviceauth/usercode"
DEVICE_TOKEN_URL = "https://auth.openai.com/api/accounts/deviceauth/token"
OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
OAUTH_SCOPE = "openid profile email"
REDIRECT_URI = "https://auth.openai.com/deviceauth/callback"
VERIFICATION_URI = "https://auth.openai.com/codex/device"


def _first_text(source: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def model_ids(payload: Any) -> tuple[str, ...]:
    """Read advertised model identifiers, skipping hidden and unsupported entries."""
    root = payload if isinstance(payload, dict) else {}
    source = root.get("models") or root.get("data") or payload
    if not isinstance(source, list):
        return ()
    seen: set[str] = set()
    found: list[str] = []
    for raw in source:
        if isinstance(raw, str):
            identifier = raw.strip()
        elif isinstance(raw, dict):
            if raw.get("supported_in_api") is False or raw.get("supportedInApi") is False:
                continue
            if str(raw.get("visibility", "")).lower() == "hidden":
                continue
            identifier = _first_text(raw, ("slug", "id", "model", "name"))
        else:
            continue
        if identifier and identifier not in seen:
            seen.add(identifier)
            found.append(identifier)
    return tuple(found)


def enrich(credential: OAuthCredential) -> OAuthCredential:
    """Fill the account identity that Codex needs from the access token claims."""
    return replace(
        credential,
        account_id=credential.account_id or chatgpt_account_id(credential.access_token),
        email=credential.email
        or jwt_claim(credential.access_token, "email", "preferred_username", "name"),
    )


def session_values(body: dict[str, Any]) -> dict[str, str]:
    """Capture the device authorization identifiers returned by OpenAI."""
    device_auth_id = _first_text(body, ("device_auth_id", "device_code"))
    user_code = _first_text(body, ("user_code", "usercode"))
    if not device_auth_id or not user_code:
        raise ValueError("OpenAI Codex device authorization response is incomplete")
    return {"device_auth_id": device_auth_id, "user_code": user_code}


def login_flow() -> DeviceCodeFlow:
    """Describe the ChatGPT device authorization grant used by ``wenyi auth login``."""
    return DeviceCodeFlow(
        provider="codex",
        display_name="ChatGPT (Codex)",
        client_id=CLIENT_ID,
        device_code_url=DEVICE_CODE_URL,
        token_url=DEVICE_TOKEN_URL,
        device_request_style="json",
        poll_request_style="json",
        session_values=session_values,
        poll_payload=lambda session: {
            "device_auth_id": session["device_auth_id"],
            "user_code": session["user_code"],
        },
        pending_statuses=frozenset({403, 404}),
        pending_on_unknown_error=True,
        exchange_url=OAUTH_TOKEN_URL,
        exchange_redirect_uri=REDIRECT_URI,
        fallback_verification_url=VERIFICATION_URI,
        enrich=enrich,
    )


class CodexClient(ResponsesClient):
    """ChatGPT subscription access through the Codex Responses endpoint."""

    default_api_key_env = DEFAULT_API_KEY_ENV
    display_name = "ChatGPT (Codex)"

    @classmethod
    def output_limit(cls, options: BaseModel, hint: int | None, explicit: int | None) -> int | None:
        """Reject explicit output limits: the subscription endpoint does not accept them."""
        if explicit is not None:
            raise ValueError(
                "The Codex subscription endpoint does not accept max_output_tokens; "
                "remove max_output_tokens from this model profile"
            )
        return None

    @classmethod
    def login_flow(cls) -> DeviceCodeFlow:
        return login_flow()

    def credential_headers(self, credential: OAuthCredential) -> dict[str, str]:
        """Identify the account and pin the request to one prompt-cache shard."""
        headers = {
            "session-id": self.cache_key,
            "thread-id": self.cache_key,
            "x-client-request-id": self.cache_key,
        }
        account_id = credential.account_id or chatgpt_account_id(credential.access_token)
        if account_id:
            headers[ACCOUNT_HEADER] = account_id
        return headers

    def available_models(self, credential: OAuthCredential) -> tuple[str, ...]:
        """List the models the subscription currently exposes to Codex clients."""
        with self.http_client() as client:
            response = client.get(
                f"{self.resolve_base_url()}{MODELS_PATH}",
                headers={**self.auth_headers(), "accept": "application/json"},
            )
        if not 200 <= response.status_code < 300:
            return ()
        try:
            payload = response.json()
        except ValueError:
            return ()
        return model_ids(payload)


__all__ = [
    "ACCOUNT_HEADER",
    "CLIENT_ID",
    "DEFAULT_API_KEY_ENV",
    "MODELS_PATH",
    "CodexClient",
    "enrich",
    "login_flow",
    "model_ids",
    "session_values",
]
