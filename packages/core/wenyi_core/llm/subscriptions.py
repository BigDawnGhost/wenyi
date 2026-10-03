"""Interactive subscription sign-in and refresh for OAuth-backed providers.

One record per provider captures the grant, the token endpoints and the refresh request.
The CLI drives the flow; adapters only ask for a usable access token, so no protocol code
lives in the CLI and no vendor endpoint lives in the transports.

Credentials are never written to disk by Wenyi: a finished login prints a shell assignment
that the user exports, matching how Wenyi reads every other credential.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .oauth.credentials import (
    OAuthCredential,
    OAuthCredentialError,
    format_env_export,
    optional_text,
    refresh_access_token,
)
from .oauth.flows import (
    AuthorizationCodeFlow,
    DeviceCodeFlow,
    DeviceCodePrompt,
    authorization_code_login,
    device_code_login,
)

FORM_STYLE = "form"
JSON_STYLE = "json"


@dataclass(frozen=True)
class Subscription:
    """One provider's interactive grant plus its token refresh."""

    kind: str
    display_name: str
    env_var: str
    flow: Callable[[], DeviceCodeFlow | AuthorizationCodeFlow]
    refresh: Callable[[OAuthCredential], OAuthCredential] | None = None
    models: Callable[[OAuthCredential, str], Sequence[str]] | None = None
    # Credential files written by the first-party client, used when the user names no file.
    credential_files: Callable[[], tuple[Path, ...]] | None = None
    notes: str = ""


# ── ChatGPT / Codex ──────────────────────────────────────────────────────────────────────
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_SCOPE = "openid profile email"


def _codex_flow() -> DeviceCodeFlow:
    from .providers.codex import login_flow

    return login_flow()


def _codex_credential_files() -> tuple[Path, ...]:
    """Locate the credential file the Codex CLI writes, honouring ``CODEX_HOME``."""
    home = os.environ.get("CODEX_HOME", "").strip() or str(Path.home() / ".codex")
    return (Path(home).expanduser() / "auth.json",)


def _codex_refresh(credential: OAuthCredential) -> OAuthCredential:
    return refresh_access_token(
        CODEX_TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "client_id": CODEX_CLIENT_ID,
            "refresh_token": credential.refresh_token or "",
            "scope": CODEX_SCOPE,
        },
        credential,
    )


# ── xAI / Grok ───────────────────────────────────────────────────────────────────────────
XAI_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
XAI_SCOPE = "openid profile email offline_access grok-cli:access api:access"
XAI_DEVICE_URL = "https://auth.x.ai/oauth2/device/code"
XAI_TOKEN_URL = "https://auth.x.ai/oauth2/token"


def _xai_flow() -> DeviceCodeFlow:
    return DeviceCodeFlow(
        provider="xai",
        display_name="xAI (Grok)",
        client_id=XAI_CLIENT_ID,
        device_code_url=XAI_DEVICE_URL,
        token_url=XAI_TOKEN_URL,
        scope=XAI_SCOPE,
        pending_statuses=frozenset({400, 428}),
        enrich=_email_from_token,
    )


def _xai_refresh(credential: OAuthCredential) -> OAuthCredential:
    return refresh_access_token(
        XAI_TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "client_id": XAI_CLIENT_ID,
            "refresh_token": credential.refresh_token or "",
            "scope": XAI_SCOPE,
        },
        credential,
    )


# ── Nous Portal ──────────────────────────────────────────────────────────────────────────
NOUS_PORTAL_ENV = "WENYI_NOUS_PORTAL_URL"
NOUS_CLIENT_ENV = "WENYI_NOUS_CLIENT_ID"
DEFAULT_NOUS_PORTAL_URL = "https://portal.nousresearch.com"
DEFAULT_NOUS_CLIENT_ID = "hermes-cli"
NOUS_SCOPE = "inference:invoke"


def _nous_portal_url() -> str:
    return os.environ.get(NOUS_PORTAL_ENV, "").strip().rstrip("/") or DEFAULT_NOUS_PORTAL_URL


def _nous_client_id() -> str:
    return os.environ.get(NOUS_CLIENT_ENV, "").strip() or DEFAULT_NOUS_CLIENT_ID


def _nous_flow() -> DeviceCodeFlow:
    portal = _nous_portal_url()
    return DeviceCodeFlow(
        provider="nous",
        display_name="Nous Research",
        client_id=_nous_client_id(),
        device_code_url=f"{portal}/api/oauth/device/code",
        token_url=f"{portal}/api/oauth/token",
        scope=NOUS_SCOPE,
        enrich=_email_from_token,
    )


def _nous_refresh(credential: OAuthCredential) -> OAuthCredential:
    return refresh_access_token(
        f"{_nous_portal_url()}/api/oauth/token",
        {
            "grant_type": "refresh_token",
            "client_id": _nous_client_id(),
            "refresh_token": credential.refresh_token or "",
        },
        credential,
    )


# ── Qwen Portal ──────────────────────────────────────────────────────────────────────────
QWEN_CLIENT_ID = "f0304373b74a44d2b584a3fb70ca9e56"
QWEN_TOKEN_URL = "https://chat.qwen.ai/api/v1/oauth2/token"


def _qwen_credential_files() -> tuple[Path, ...]:
    """Locate the credential file the Qwen CLI writes."""
    return (Path.home() / ".qwen" / "oauth_creds.json",)


def _qwen_refresh(credential: OAuthCredential) -> OAuthCredential:
    return refresh_access_token(
        QWEN_TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "client_id": QWEN_CLIENT_ID,
            "refresh_token": credential.refresh_token or "",
        },
        credential,
    )


def _qwen_flow() -> AuthorizationCodeFlow:
    raise OAuthCredentialError(
        "Qwen Portal has no scriptable sign-in; run `qwen auth` once and then import the "
        "written credential with `wenyi auth import qwen-oauth`"
    )


# ── MiniMax ──────────────────────────────────────────────────────────────────────────────
MINIMAX_CLIENT_ID = "78257093-7e40-4613-99e0-527b14b39113"
MINIMAX_SCOPE = "group_id profile model.completion"
MINIMAX_USER_CODE_GRANT = "urn:ietf:params:oauth:grant-type:user_code"
MINIMAX_GLOBAL_BASE = "https://api.minimax.io"
MINIMAX_CN_BASE = "https://api.minimaxi.com"


def _minimax_base() -> str:
    region = os.environ.get("WENYI_MINIMAX_REGION", "global").strip().lower()
    return MINIMAX_CN_BASE if region in {"cn", "china"} else MINIMAX_GLOBAL_BASE


def _minimax_flow() -> DeviceCodeFlow:
    base = _minimax_base()
    return DeviceCodeFlow(
        provider="minimax-oauth",
        display_name="MiniMax",
        client_id=MINIMAX_CLIENT_ID,
        device_code_url=f"{base}/oauth/code",
        token_url=f"{base}/oauth/token",
        scope=MINIMAX_SCOPE,
        device_request_style=FORM_STYLE,
        poll_request_style=FORM_STYLE,
        poll_payload=lambda session: {
            "grant_type": MINIMAX_USER_CODE_GRANT,
            "client_id": MINIMAX_CLIENT_ID,
            "user_code": session["user_code"],
            "code_verifier": session.get("code_verifier", ""),
        },
        pending_on_unknown_error=True,
        enrich=_minimax_enrich,
    )


def _minimax_enrich(credential: OAuthCredential) -> OAuthCredential:
    return credential


def _minimax_refresh(credential: OAuthCredential) -> OAuthCredential:
    return refresh_access_token(
        f"{_minimax_base()}/oauth/token",
        {
            "grant_type": "refresh_token",
            "client_id": MINIMAX_CLIENT_ID,
            "refresh_token": credential.refresh_token or "",
        },
        credential,
    )


# ── GitHub Copilot ───────────────────────────────────────────────────────────────────────
COPILOT_CLIENT_ID = "Iv1.b507a08c87ecfe98"
COPILOT_DEVICE_URL = "https://github.com/login/device/code"
COPILOT_ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
COPILOT_TOKEN_EXCHANGE_URL = "https://api.github.com/copilot_internal/v2/token"


def _copilot_flow() -> DeviceCodeFlow:
    return DeviceCodeFlow(
        provider="copilot",
        display_name="GitHub Copilot",
        client_id=COPILOT_CLIENT_ID,
        device_code_url=COPILOT_DEVICE_URL,
        token_url=COPILOT_ACCESS_TOKEN_URL,
        scope="read:user",
        pending_statuses=frozenset(),
        exchange_url=None,
        fallback_verification_url="https://github.com/login/device",
        enrich=_email_from_token,
    )


def _copilot_refresh(credential: OAuthCredential) -> OAuthCredential:
    """Exchange the stored GitHub token for a fresh short-lived Copilot token."""
    from .providers._credentials import exchange_copilot_token

    return exchange_copilot_token(credential)


# ── Google Antigravity ───────────────────────────────────────────────────────────────────
# The desktop client Antigravity signs in with is Google's; Wenyi never bundles its OAuth
# client, so the pair is read from the environment like every other credential.
ANTIGRAVITY_CLIENT_ID_ENV = "WENYI_ANTIGRAVITY_CLIENT_ID"
ANTIGRAVITY_CLIENT_SECRET_ENV = "WENYI_ANTIGRAVITY_CLIENT_SECRET"
ANTIGRAVITY_AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
ANTIGRAVITY_TOKEN_URL = "https://oauth2.googleapis.com/token"
ANTIGRAVITY_SCOPES = (
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/cclog",
    "https://www.googleapis.com/auth/experimentsandconfigs",
)
DEFAULT_ANTIGRAVITY_PROJECT = "bamboo-precept-lgxtn"


def _antigravity_client() -> tuple[str, str]:
    """Return the configured Google OAuth client, or explain how to supply one."""
    client_id = os.environ.get(ANTIGRAVITY_CLIENT_ID_ENV, "").strip()
    client_secret = os.environ.get(ANTIGRAVITY_CLIENT_SECRET_ENV, "").strip()
    if not client_id or not client_secret:
        raise OAuthCredentialError(
            "Antigravity sign-in needs a Google OAuth client: set "
            f"{ANTIGRAVITY_CLIENT_ID_ENV} and {ANTIGRAVITY_CLIENT_SECRET_ENV} to the client "
            "the Antigravity CLI uses, for example in the .env beside the configuration file, "
            "then sign in again"
        )
    return client_id, client_secret


def _antigravity_flow() -> AuthorizationCodeFlow:
    client_id, client_secret = _antigravity_client()
    return AuthorizationCodeFlow(
        provider="antigravity",
        display_name="Google Antigravity",
        client_id=client_id,
        client_secret=client_secret,
        authorization_url=ANTIGRAVITY_AUTHORIZATION_URL,
        token_url=ANTIGRAVITY_TOKEN_URL,
        scopes=ANTIGRAVITY_SCOPES,
        redirect_port=45297,
        redirect_path="/oauth2callback",
        extra_authorization_params={"access_type": "offline", "prompt": "consent"},
        enrich=_antigravity_enrich,
    )


def _antigravity_enrich(credential: OAuthCredential) -> OAuthCredential:
    project = os.environ.get("WENYI_ANTIGRAVITY_PROJECT", "").strip()
    credential = _email_from_token(credential)
    return replace(
        credential, project_id=credential.project_id or project or DEFAULT_ANTIGRAVITY_PROJECT
    )


def _antigravity_refresh(credential: OAuthCredential) -> OAuthCredential:
    client_id, client_secret = _antigravity_client()
    return refresh_access_token(
        ANTIGRAVITY_TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": credential.refresh_token or "",
        },
        credential,
    )


def _email_from_token(credential: OAuthCredential) -> OAuthCredential:
    from .oauth.credentials import jwt_claim

    if credential.email:
        return credential
    return replace(
        credential,
        email=jwt_claim(credential.access_token, "email", "preferred_username", "name"),
    )


SUBSCRIPTIONS: dict[str, Subscription] = {
    "codex": Subscription(
        kind="openai-codex",
        display_name="ChatGPT (Codex)",
        env_var="WENYI_CODEX_OAUTH",
        flow=_codex_flow,
        refresh=_codex_refresh,
        credential_files=_codex_credential_files,
        notes="ChatGPT Plus/Pro subscription through the Codex Responses endpoint.",
    ),
    "xai": Subscription(
        kind="xai",
        display_name="xAI (Grok)",
        env_var="WENYI_XAI_OAUTH",
        flow=_xai_flow,
        refresh=_xai_refresh,
        notes="Grok OAuth device grant; requires an eligible xAI subscription.",
    ),
    "nous": Subscription(
        kind="nous",
        display_name="Nous Research",
        env_var="WENYI_NOUS_OAUTH",
        flow=_nous_flow,
        refresh=_nous_refresh,
        notes="Nous Portal device grant; override the portal or client ID with "
        "WENYI_NOUS_PORTAL_URL / WENYI_NOUS_CLIENT_ID.",
    ),
    "qwen": Subscription(
        kind="qwen-oauth",
        display_name="Qwen Portal",
        env_var="WENYI_QWEN_OAUTH",
        flow=_qwen_flow,
        refresh=_qwen_refresh,
        credential_files=_qwen_credential_files,
        notes="Import the credential written by the Qwen CLI.",
    ),
    "minimax": Subscription(
        kind="minimax-oauth",
        display_name="MiniMax",
        env_var="WENYI_MINIMAX_OAUTH",
        flow=_minimax_flow,
        refresh=_minimax_refresh,
        notes="Set WENYI_MINIMAX_REGION=cn for the mainland-China portal.",
    ),
    "copilot": Subscription(
        kind="copilot",
        display_name="GitHub Copilot",
        env_var="WENYI_COPILOT_OAUTH",
        flow=_copilot_flow,
        refresh=_copilot_refresh,
        notes="Stores a GitHub token; the Copilot token is exchanged on first use.",
    ),
    "antigravity": Subscription(
        kind="antigravity",
        display_name="Google Antigravity",
        env_var="WENYI_ANTIGRAVITY_OAUTH",
        flow=_antigravity_flow,
        refresh=_antigravity_refresh,
        notes="Requires WENYI_ANTIGRAVITY_CLIENT_ID and WENYI_ANTIGRAVITY_CLIENT_SECRET; set "
        "WENYI_ANTIGRAVITY_PROJECT to override the Cloud Code project ID.",
    ),
}

SUBSCRIPTIONS_BY_KIND: dict[str, Subscription] = {
    subscription.kind: subscription for subscription in SUBSCRIPTIONS.values()
}


def get_subscription(kind: str) -> Subscription:
    """Return the subscription for a login name, provider kind or provider alias."""
    subscription = find_subscription(kind)
    if subscription is None:
        raise OAuthCredentialError(f"Provider {kind} has no subscription sign-in") from None
    return subscription


def find_subscription(name: str) -> Subscription | None:
    """Resolve a login name, provider kind or provider alias; ``None`` when unknown."""
    key = (name or "").strip().lower()
    if not key:
        return None
    known = SUBSCRIPTIONS.get(key) or SUBSCRIPTIONS_BY_KIND.get(key)
    if known is not None:
        return known
    from .profiles import get_profile

    try:
        profile = get_profile(key)
    except ValueError:
        return None
    return SUBSCRIPTIONS_BY_KIND.get(profile.kind)


def subscription_for_env(env_var: str) -> Subscription | None:
    """Return the subscription whose credential environment variable matches."""
    for subscription in SUBSCRIPTIONS.values():
        if subscription.env_var == env_var:
            return subscription
    return None


def login(
    kind: str,
    *,
    on_prompt: Callable[[DeviceCodePrompt], None] | None = None,
    on_authorize: Callable[[str], None] | None = None,
    open_browser: bool = True,
    port: int | None = None,
    timeout: float | None = None,
) -> OAuthCredential:
    """Run the interactive grant for one subscription and return the credential."""
    subscription = get_subscription(kind)
    flow = subscription.flow()
    if isinstance(flow, AuthorizationCodeFlow):
        if port is not None:
            flow = replace(flow, redirect_port=port)
        return authorization_code_login(
            flow,
            on_authorize=on_authorize or (lambda url: None),
            open_browser=open_browser,
            **({"timeout": timeout} if timeout is not None else {}),
        )
    device_flow = flow
    if port is not None:
        raise OAuthCredentialError("--port applies only to browser-based sign-in")
    return device_code_login(
        device_flow,
        on_prompt=on_prompt or (lambda prompt: None),
        open_browser=open_browser,
        **({"timeout": timeout} if timeout is not None else {}),
    )


def refresh(kind: str, credential: OAuthCredential) -> OAuthCredential:
    """Refresh a stored credential for one provider kind."""
    subscription = get_subscription(kind)
    if subscription.refresh is None:
        raise OAuthCredentialError(
            f"{subscription.display_name} credentials cannot be refreshed; sign in again"
        )
    return subscription.refresh(credential)


def export_line(env_var: str, credential: OAuthCredential) -> str:
    """Return the shell export the user pastes to store a credential."""
    return format_env_export(env_var, credential)


def default_models(kind: str) -> tuple[str, ...]:
    """Return the models a profile advertises without a live catalog."""
    from .profiles import get_profile

    return get_profile(kind).fallback_models


def normalize_payload(kind: str, payload: dict[str, Any]) -> OAuthCredential:
    """Build a credential from a provider token response."""
    access_token = optional_text(payload.get("access_token")) or ""
    if not access_token:
        raise OAuthCredentialError(f"{kind} token response did not include an access token")
    return OAuthCredential(access_token=access_token).with_token_response(payload)


__all__ = [
    "SUBSCRIPTIONS",
    "SUBSCRIPTIONS_BY_KIND",
    "Subscription",
    "default_models",
    "export_line",
    "find_subscription",
    "get_subscription",
    "login",
    "normalize_payload",
    "refresh",
    "subscription_for_env",
]
