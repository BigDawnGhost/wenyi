"""Interactive subscription login flows.

Two grants are implemented: the RFC 8628 device authorization grant used by ChatGPT
(Codex) and xAI (Grok), and the RFC 7636 authorization code grant with PKCE used by
Google (Antigravity). Both return a credential for the caller to export; no token is
written to disk.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import webbrowser
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Literal
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from .credentials import (
    OAuthCredential,
    OAuthCredentialError,
    optional_number,
    optional_text,
    parse_json_object,
)

DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
DEFAULT_DEVICE_TIMEOUT_SECONDS = 900.0
DEFAULT_AUTHORIZATION_TIMEOUT_SECONDS = 300.0
DEFAULT_POLL_INTERVAL_SECONDS = 5.0
SLOW_DOWN_PENALTY_SECONDS = 5.0
TOKEN_REQUEST_TIMEOUT_SECONDS = 30.0

Callback = Callable[[OAuthCredential], OAuthCredential]


@dataclass(frozen=True)
class DeviceCodePrompt:
    """Instructions shown to the user while waiting for device authorization."""

    provider: str
    display_name: str
    user_code: str
    verification_url: str
    expires_in: float
    interval: float


@dataclass(frozen=True)
class DeviceCodeFlow:
    """Provider-specific parameters of one device authorization grant."""

    provider: str
    display_name: str
    client_id: str
    device_code_url: str
    token_url: str
    scope: str | None = None
    device_request_style: Literal["form", "json"] = "form"
    poll_request_style: Literal["form", "json"] = "form"
    poll_endpoint: str | None = None
    session_values: Callable[[Mapping[str, Any]], dict[str, str]] | None = None
    poll_payload: Callable[[Mapping[str, str]], dict[str, Any]] | None = None
    pending_statuses: frozenset[int] = frozenset()
    pending_on_unknown_error: bool = False
    exchange_url: str | None = None
    exchange_redirect_uri: str | None = None
    fallback_verification_url: str | None = None
    enrich: Callback | None = None
    headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def poll_url(self) -> str:
        return self.poll_endpoint or self.token_url


@dataclass(frozen=True)
class AuthorizationCodeFlow:
    """Provider-specific parameters of one authorization code grant with PKCE."""

    provider: str
    display_name: str
    client_id: str
    authorization_url: str
    token_url: str
    scopes: tuple[str, ...]
    client_secret: str | None = None
    extra_authorization_params: Mapping[str, str] = field(default_factory=dict)
    redirect_host: str = "127.0.0.1"
    redirect_port: int = 45297
    redirect_path: str = "/oauth2callback"
    enrich: Callback | None = None

    @property
    def redirect_uri(self) -> str:
        return f"http://{self.redirect_host}:{self.redirect_port}{self.redirect_path}"


def pkce_pair() -> tuple[str, str]:
    """Return a PKCE ``(verifier, challenge)`` pair using the S256 transformation."""
    verifier = _base64_url(secrets.token_bytes(64))
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, _base64_url(digest)


def build_authorization_url(flow: AuthorizationCodeFlow, *, state: str, code_challenge: str) -> str:
    """Build the browser authorization URL for an authorization code grant."""
    params: dict[str, str] = {
        "client_id": flow.client_id,
        "response_type": "code",
        "redirect_uri": flow.redirect_uri,
        "scope": " ".join(flow.scopes),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        **dict(flow.extra_authorization_params),
    }
    return f"{flow.authorization_url}?{urlencode(params)}"


def device_code_login(
    flow: DeviceCodeFlow,
    *,
    on_prompt: Callable[[DeviceCodePrompt], None],
    open_browser: bool = True,
    timeout: float = DEFAULT_DEVICE_TIMEOUT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> OAuthCredential:
    """Run a device authorization grant until the user approves or the code expires."""
    status, body = _post(
        flow.device_code_url,
        _device_payload(flow),
        style=flow.device_request_style,
        headers=flow.headers,
    )
    if not 200 <= status < 300:
        raise OAuthCredentialError(
            f"{flow.display_name} device authorization request failed (HTTP {status}): "
            f"{_detail(body)}"
        )

    session = _session_values(flow, body)
    user_code = optional_text(body.get("user_code")) or optional_text(body.get("usercode")) or ""
    verification_url = (
        optional_text(body.get("verification_uri"))
        or optional_text(body.get("verification_url"))
        or flow.fallback_verification_url
        or ""
    )
    if not user_code or not verification_url:
        raise OAuthCredentialError(
            f"{flow.display_name} device authorization response is incomplete"
        )

    expires_in = max(1.0, optional_number(body.get("expires_in")) or 900.0)
    interval = max(1.0, optional_number(body.get("interval")) or DEFAULT_POLL_INTERVAL_SECONDS)
    deadline = time.monotonic() + min(timeout, expires_in)
    on_prompt(
        DeviceCodePrompt(
            provider=flow.provider,
            display_name=flow.display_name,
            user_code=user_code,
            verification_url=verification_url,
            expires_in=expires_in,
            interval=interval,
        )
    )

    if open_browser:
        _open_browser(optional_text(body.get("verification_uri_complete")) or verification_url)

    while True:
        sleep(interval)
        if time.monotonic() >= deadline:
            raise OAuthCredentialError(
                f"{flow.display_name} device authorization expired before it was approved"
            )
        outcome, credential, message = _poll_once(flow, session)
        if outcome == "completed" and credential is not None:
            return _enrich(flow, credential)
        if outcome == "slow-down":
            interval += SLOW_DOWN_PENALTY_SECONDS
            continue
        if outcome == "pending":
            continue
        raise OAuthCredentialError(message or f"{flow.display_name} authorization failed")


def authorization_code_login(
    flow: AuthorizationCodeFlow,
    *,
    on_authorize: Callable[[str], None],
    open_browser: bool = True,
    timeout: float = DEFAULT_AUTHORIZATION_TIMEOUT_SECONDS,
    wait_for_code: Callable[[AuthorizationCodeFlow, str, float], str] | None = None,
) -> OAuthCredential:
    """Run an authorization code grant with PKCE and a loopback redirect."""
    state = _base64_url(secrets.token_bytes(24))
    verifier, challenge = pkce_pair()
    url = build_authorization_url(flow, state=state, code_challenge=challenge)

    def authorize() -> None:
        on_authorize(url)
        if open_browser:
            _open_browser(url)

    if wait_for_code is not None:
        authorize()
        code = wait_for_code(flow, state, timeout)
    else:
        # Bind the callback listener before opening the browser, including fast approvals.
        code = wait_for_redirect(flow, state, timeout, on_ready=authorize)
    status, body = _post(
        flow.token_url,
        {
            "client_id": flow.client_id,
            "code": code,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": flow.redirect_uri,
            **({"client_secret": flow.client_secret} if flow.client_secret else {}),
        },
        style="form",
    )
    if not 200 <= status < 300:
        raise OAuthCredentialError(
            f"{flow.display_name} token exchange failed (HTTP {status}): {_detail(body)}"
        )
    access_token = optional_text(body.get("access_token"))
    if not access_token:
        raise OAuthCredentialError(
            f"{flow.display_name} token exchange did not return an access token"
        )
    return _enrich(flow, OAuthCredential(access_token=access_token).with_token_response(body))


def wait_for_redirect(
    flow: AuthorizationCodeFlow,
    expected_state: str,
    timeout: float,
    *,
    on_ready: Callable[[], None] | None = None,
) -> str:
    """Serve one loopback redirect and return the authorization code it carries."""
    received: dict[str, str] = {}
    ready = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        # A browser may keep HTTP/1.1 connections open indefinitely, blocking shutdown.
        protocol_version = "HTTP/1.0"

        def do_GET(self) -> None:  # noqa: N802 - name required by BaseHTTPRequestHandler
            parsed = urlsplit(self.path)
            if parsed.path != flow.redirect_path:
                self.send_error(404)
                return
            query = parse_qs(parsed.query)
            values = {key: items[0] for key, items in query.items() if items}
            if values.get("state") != expected_state:
                self.send_error(400, "OAuth state mismatch; return to the original login tab")
                return
            if not values.get("code") and not values.get("error"):
                self.send_error(400, "Missing OAuth authorization code")
                return
            received.update(values)
            body = (_CALLBACK_PAGE if not values.get("error") else _CALLBACK_ERROR_PAGE).encode(
                "utf-8"
            )
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.send_header("connection", "close")
            self.end_headers()
            # Signal completion even if the browser closes the tab during the response.
            ready.set()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True

        def log_message(self, *_args: Any) -> None:
            """Silence the default stderr access log."""

    try:
        server = ThreadingHTTPServer((flow.redirect_host, flow.redirect_port), Handler)
    except OSError as error:
        raise OAuthCredentialError(
            f"Cannot listen on {flow.redirect_host}:{flow.redirect_port} for the OAuth "
            f"redirect ({error.strerror or error}); free that port or pass --port"
        ) from None
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if on_ready is not None:
            on_ready()
        if not ready.wait(timeout):
            raise OAuthCredentialError(
                f"Timed out after {int(timeout)}s waiting for the {flow.display_name} redirect"
            )
    finally:
        server.shutdown()
        server.server_close()

    error = received.get("error")
    if error:
        raise OAuthCredentialError(
            f"{flow.display_name} authorization failed: {received.get('error_description', error)}"
        )
    if received.get("state") != expected_state:
        raise OAuthCredentialError(
            "The OAuth redirect state did not match the request; retry the login"
        )
    code = received.get("code")
    if not code:
        raise OAuthCredentialError("The OAuth redirect did not include an authorization code")
    return code


def _device_payload(flow: DeviceCodeFlow) -> dict[str, Any]:
    payload: dict[str, Any] = {"client_id": flow.client_id}
    if flow.scope:
        payload["scope"] = flow.scope
    return payload


def _session_values(flow: DeviceCodeFlow, body: Mapping[str, Any]) -> dict[str, str]:
    if flow.session_values is not None:
        return flow.session_values(body)
    device_code = optional_text(body.get("device_code"))
    if not device_code:
        raise OAuthCredentialError(f"{flow.display_name} device response has no device code")
    return {"device_code": device_code}


def _poll_once(
    flow: DeviceCodeFlow, session: Mapping[str, str]
) -> tuple[str, OAuthCredential | None, str | None]:
    payload = (
        flow.poll_payload(session)
        if flow.poll_payload is not None
        else {"grant_type": DEVICE_GRANT_TYPE, "client_id": flow.client_id, **session}
    )
    status, body = _post(
        flow.poll_url, payload, style=flow.poll_request_style, headers=flow.headers
    )
    code = _error_code(body)
    message = _detail(body)
    if code in {"authorization_pending", "pending", "waiting", "in_progress"}:
        return "pending", None, message
    if code == "slow_down":
        return "slow-down", None, message
    if code in {"expired_token", "expired"}:
        return "failed", None, message or "device authorization code expired"
    if code in {"access_denied", "denied"}:
        return "failed", None, message or "authorization was denied"
    if 200 <= status < 300 and not code:
        access_token = optional_text(body.get("access_token"))
        if access_token:
            credential = OAuthCredential(access_token=access_token).with_token_response(body)
            return "completed", credential, None
        authorization_code = optional_text(body.get("authorization_code"))
        code_verifier = optional_text(body.get("code_verifier"))
        if authorization_code and code_verifier and flow.exchange_url:
            credential = _exchange_device_code(flow, authorization_code, code_verifier)
            return "completed", credential, None
        return (
            "failed",
            None,
            f"{flow.display_name} authorization response is incomplete: {_detail(body)}",
        )

    if status in flow.pending_statuses:
        return "pending", None, message
    if not code and flow.pending_on_unknown_error and status >= 400:
        return "pending", None, message
    return "failed", None, message or f"{flow.display_name} device authorization failed"


def _exchange_device_code(
    flow: DeviceCodeFlow, authorization_code: str, code_verifier: str
) -> OAuthCredential:
    status, body = _post(
        flow.exchange_url or flow.token_url,
        {
            "grant_type": "authorization_code",
            "code": authorization_code,
            "redirect_uri": flow.exchange_redirect_uri or "",
            "client_id": flow.client_id,
            "code_verifier": code_verifier,
        },
        style="form",
        headers=flow.headers,
    )
    access_token = optional_text(body.get("access_token"))
    if not 200 <= status < 300 or not access_token:
        raise OAuthCredentialError(
            f"{flow.display_name} authorization code exchange failed: {_detail(body)}"
        )
    return OAuthCredential(access_token=access_token).with_token_response(body)


def _enrich(
    flow: DeviceCodeFlow | AuthorizationCodeFlow, credential: OAuthCredential
) -> OAuthCredential:
    return flow.enrich(credential) if flow.enrich is not None else credential


def _post(
    url: str,
    payload: Mapping[str, Any],
    *,
    style: Literal["form", "json"],
    headers: Mapping[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    request_headers = {
        "accept": "application/json",
        "content-type": (
            "application/json" if style == "json" else "application/x-www-form-urlencoded"
        ),
        **dict(headers or {}),
    }
    body = (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if style == "json"
        else urlencode({key: str(value) for key, value in payload.items()})
    )
    try:
        with httpx.Client(timeout=TOKEN_REQUEST_TIMEOUT_SECONDS) as client:
            response = client.post(url, content=body, headers=request_headers)
    except httpx.HTTPError:
        raise OAuthCredentialError(
            "OAuth service request failed; check your network and retry sign-in"
        ) from None
    return response.status_code, parse_json_object(response.text)


def _error_code(body: Mapping[str, Any]) -> str:
    direct = optional_text(body.get("error"))
    if direct:
        return direct
    nested = body.get("error")
    if isinstance(nested, Mapping):
        return optional_text(nested.get("code")) or optional_text(nested.get("type")) or ""
    for key in ("code", "status", "state"):
        value = optional_text(body.get(key))
        if value:
            return value
    return ""


def _detail(body: Mapping[str, Any]) -> str:
    nested = body.get("error")
    nested_message = optional_text(nested.get("message")) if isinstance(nested, Mapping) else None
    nested_code = optional_text(nested.get("code")) if isinstance(nested, Mapping) else None
    return (
        optional_text(body.get("error_description"))
        or optional_text(body.get("message"))
        or nested_message
        or nested_code
        or optional_text(body.get("error"))
        or "no error detail returned"
    )


def _base64_url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _open_browser(url: str) -> None:
    """Keep the printed URL usable when no desktop browser is available."""
    try:
        webbrowser.open(url)
    except (webbrowser.Error, OSError):
        pass


_CALLBACK_ERROR_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Wenyi authorization failed</title></head>
<body><h1>Authorization failed</h1><p>Return to the terminal for details.</p></body></html>
"""


_CALLBACK_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Wenyi authorization complete</title></head>
<body style="font-family: system-ui, sans-serif; margin: 4rem">
<h1>Authorization received</h1>
<p>Return to the terminal while Wenyi finishes exchanging the authorization code.</p>
</body></html>
"""
