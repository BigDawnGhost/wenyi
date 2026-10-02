"""Shared credential handling for subscription OAuth providers.

An adapter reads its credential from the environment variable named by the connection,
refreshes it in memory when it expires, and never writes it back to disk. Protocols stay
in the concrete provider modules; only credential lifetime, streaming and error mapping
are shared here.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..oauth.credentials import (
    OAuthCredential,
    OAuthCredentialError,
    load_credential,
)
from ..transport import ProviderAdapter, RequestContext
from ..usage import UsageSample, make_usage_sample, read_usage_int

DEFAULT_HTTP_TIMEOUT_SECONDS = 600.0
MAX_ERROR_BODY_CHARS = 600


@dataclass(frozen=True)
class AccountReport:
    """Locally gathered state of one subscription account for ``wenyi auth check``."""

    provider: str
    display_name: str
    email: str | None = None
    plan: str | None = None
    models: Sequence[str] = field(default_factory=tuple)
    notes: Sequence[str] = field(default_factory=tuple)


class OAuthProviderAdapter(ProviderAdapter):
    """Base class for providers authenticated by an environment OAuth credential."""

    requires_api_key = True
    display_name = "OAuth subscription"
    default_http_timeout = DEFAULT_HTTP_TIMEOUT_SECONDS

    def __init__(self, cfg):
        super().__init__(cfg)
        self._credential: OAuthCredential | None = None
        self._credential_lock = threading.Lock()

    @property
    def credential_env(self) -> str:
        """Environment variable holding the credential for this connection."""
        name = self.api_key_env or self.default_api_key_env
        if not name:
            raise OAuthCredentialError(f"Provider {self.cfg.kind} needs api_key_env set")
        return name

    def validate_credentials(self) -> None:
        """Fail fast with an actionable message when the credential cannot be used."""
        self.credential()

    def credential(self) -> OAuthCredential:
        """Return a usable credential, refreshing it in memory when it has expired."""
        with self._credential_lock:
            if self._credential is None:
                self._credential = load_credential(self.credential_env, self.cfg.kind)
            credential = self._credential
            if credential.expired():
                if not credential.refreshable():
                    raise OAuthCredentialError(
                        f"The {self.display_name} credential in {self.credential_env} has "
                        f"expired and has no refresh token; run `wenyi auth login "
                        f"{self.cfg.kind}` again"
                    )
                credential = self._refresh(credential)
                self._credential = credential
            if not credential.access_token:
                raise OAuthCredentialError(
                    f"The {self.display_name} credential in {self.credential_env} has no "
                    f"access token; run `wenyi auth login {self.cfg.kind}` again"
                )
            return credential

    def _refresh(self, credential: OAuthCredential) -> OAuthCredential:
        raise OAuthCredentialError(f"Provider {self.cfg.kind} cannot refresh credentials")

    def available_models(self, credential: OAuthCredential) -> Sequence[str]:
        """Return model IDs advertised by the subscription; empty when unsupported."""
        return ()

    def account_report(self) -> AccountReport:
        """Describe the configured account and its reachable models."""
        credential = self.credential()
        return AccountReport(
            provider=self.cfg.kind,
            display_name=self.display_name,
            email=credential.email,
            plan=credential.plan,
            models=tuple(self.available_models(credential)),
        )

    def http_client(self) -> httpx.Client:
        """Create a client with the connection timeout; callers close it."""
        return httpx.Client(timeout=self.cfg.timeout or self.default_http_timeout)

    def auth_headers(self, credential: OAuthCredential, **extra: str) -> dict[str, str]:
        """Build bearer authorization headers for subscription endpoints."""
        return {"authorization": f"Bearer {credential.access_token}", **extra}


def auth_failure(provider: str, status: int, body: str = "") -> RuntimeError:
    """Build the permanent error used when a subscription must be re-authorized."""
    detail = f": {body[:MAX_ERROR_BODY_CHARS]}" if body.strip() else ""
    return RuntimeError(
        f"{provider} rejected the credential (HTTP {status}){detail}; run "
        f"`wenyi auth login` for this provider to sign in again"
    )


def status_failure(response: httpx.Response, body: str = "") -> httpx.HTTPStatusError:
    """Build a retry-aware HTTP error carrying the upstream status and headers."""
    detail = body[:MAX_ERROR_BODY_CHARS].strip()
    message = f"HTTP {response.status_code} from {response.request.url}"
    if detail:
        message = f"{message}: {detail}"
    return httpx.HTTPStatusError(message, request=response.request, response=response)


def read_error_body(response: httpx.Response) -> str:
    """Read an already-loaded response body for diagnostics."""
    try:
        return response.text
    except httpx.ResponseNotRead:  # pragma: no cover - defensive
        return ""


def iter_sse_json(lines: Iterator[str]) -> Iterator[dict[str, Any]]:
    """Yield decoded JSON objects from a ``text/event-stream`` body."""
    for line in lines:
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw or raw == "[DONE]":
            continue
        try:
            payload = json.loads(raw)
        except ValueError as error:
            raise RuntimeError("The provider returned malformed server-sent JSON") from error
        if isinstance(payload, dict):
            yield payload


def usage_sample(payload: Any, *, cache_hit_tokens: int = 0) -> UsageSample | None:
    """Build a usage sample from OpenAI-style token counters."""
    if not isinstance(payload, dict):
        return None
    prompt_tokens = read_usage_int(payload, "prompt_tokens")
    completion_tokens = read_usage_int(payload, "completion_tokens")
    total_tokens = read_usage_int(payload, "total_tokens") or (prompt_tokens + completion_tokens)
    return make_usage_sample(
        {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        },
        cache_hit_tokens=cache_hit_tokens,
        cache_miss_tokens=max(0, prompt_tokens - cache_hit_tokens),
    )


def openai_style_usage(payload: Any) -> UsageSample | None:
    """Normalize a Responses API usage object into the shared accounting shape."""
    if not isinstance(payload, dict):
        return None
    details = payload.get("input_tokens_details")
    cache_hit_tokens = read_usage_int(details, "cached_tokens") if isinstance(details, dict) else 0
    return usage_sample(
        {
            "prompt_tokens": read_usage_int(payload, "input_tokens"),
            "completion_tokens": read_usage_int(payload, "output_tokens"),
            "total_tokens": read_usage_int(payload, "total_tokens"),
        },
        cache_hit_tokens=cache_hit_tokens,
    )


def gemini_style_usage(payload: Any) -> UsageSample | None:
    """Normalize a Gemini ``usageMetadata`` object into the shared accounting shape.

    Thought tokens are billed as output, matching the native Gemini provider.
    """
    if not isinstance(payload, dict):
        return None
    return usage_sample(
        {
            "prompt_tokens": read_usage_int(payload, "promptTokenCount"),
            "completion_tokens": read_usage_int(payload, "candidatesTokenCount")
            + read_usage_int(payload, "thoughtsTokenCount"),
            "total_tokens": read_usage_int(payload, "totalTokenCount"),
        },
        cache_hit_tokens=read_usage_int(payload, "cachedContentTokenCount"),
    )


def record(context: RequestContext, sample: UsageSample | None) -> None:
    """Record an optional usage sample on the request context."""
    if sample is not None:
        context.record_usage(sample)
