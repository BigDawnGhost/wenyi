"""Shared plumbing for profile-driven wire adapters.

One adapter subclass per wire protocol. Every vendor quirk -- endpoint, credential kind,
headers, reasoning placement -- is declared by the provider profile, so an adapter never
branches on a vendor name; it only builds one request body and reads one response body.
"""

from __future__ import annotations

import time
from abc import abstractmethod
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any, Generic

import httpx
from pydantic import BaseModel

from ...configuration import ProviderConfig
from ...oauth.credentials import OAuthCredential
from ...profiles import (
    AUTH_API_KEY,
    AUTH_VERTEX,
    WIRE_ANTHROPIC,
    WIRE_GEMINI_CLOUDCODE,
    WIRE_GEMINI_NATIVE,
    WIRE_OPENAI_CHAT,
    WIRE_RESPONSES,
    ProviderProfile,
    ReasoningRequest,
    get_profile,
)
from ...transport import Messages, ProviderAdapter, RequestContext, ResolvedModel
from .._credentials import (
    candidate_env_vars,
    configured_base_url,
    read_env_value,
    resolve_credential,
    vertex_token_and_base_url,
)
from .._oauth import status_failure
from .._options import OptionsT, ProviderOptions, WireConnectionOptions

MIN_THINKING_OUTPUT_TOKENS = 4096
MAX_ERROR_BODY_CHARS = 600
EVENT_STREAM_TYPE = "text/event-stream"

# Wire protocol -> ``(module, client class)`` for the adapter that speaks it. Module names are
# relative to ``wenyi_core.llm.providers`` so the registry can import them like any other spec.
WIRE_ADAPTERS: dict[str, tuple[str, str]] = {
    WIRE_OPENAI_CHAT: ("wire.openai_chat", "OpenAIChatClient"),
    WIRE_ANTHROPIC: ("wire.anthropic", "AnthropicClient"),
    WIRE_RESPONSES: ("wire.responses", "ResponsesClient"),
    WIRE_GEMINI_NATIVE: ("gemini", "GeminiClient"),
    WIRE_GEMINI_CLOUDCODE: ("wire.gemini_cloudcode", "CloudCodeClient"),
}

# Wires served by a profile-driven adapter above; ``gemini_native`` keeps its own SDK adapter.
PROFILE_WIRES = frozenset({WIRE_OPENAI_CHAT, WIRE_ANTHROPIC, WIRE_RESPONSES, WIRE_GEMINI_CLOUDCODE})


def credential_failure(profile: ProviderProfile, status: int, body: str = "") -> RuntimeError:
    """Build the actionable error raised when a provider rejects the credential."""
    detail = f": {body[:MAX_ERROR_BODY_CHARS].strip()}" if body.strip() else ""
    if profile.auth == AUTH_API_KEY:
        env_name = profile.api_key_envs[0] if profile.api_key_envs else "the API key"
        hint = f"check {env_name}"
    else:
        hint = f"run `wenyi auth login {profile.kind}` to sign in again"
    return RuntimeError(
        f"{profile.display()} rejected the credential (HTTP {status}){detail}; {hint}"
    )


def is_event_stream(response: httpx.Response) -> bool:
    """Report whether a response carries a server-sent event stream.

    A provider may stream without declaring a content type — the Codex endpoint does — so an
    absent header is settled by inspecting the body prefix instead of the header. That buffers
    the response, which stays replayable because httpx backs a read body with a ``ByteStream``.
    """
    content_type = response.headers.get("content-type", "").lower()
    if EVENT_STREAM_TYPE in content_type:
        return True
    if content_type.strip():
        return False
    return _opens_event_stream(response.read())


def _opens_event_stream(body: bytes) -> bool:
    """Report whether a body starts with a server-sent event field."""
    for line in body.splitlines():
        text = line.strip()
        if text:
            return text.startswith((b"event:", b"data:", b"id:", b"retry:", b":"))
    return False


def merge_extra_body(body: dict[str, Any], *sources: Mapping[str, Any]) -> None:
    """Merge profile and user extra body fields over a request body, last source winning."""
    for source in sources:
        for key, value in source.items():
            current = body.get(key)
            if isinstance(current, dict) and isinstance(value, dict):
                body[key] = {**current, **value}
            else:
                body[key] = value


class ProfileAdapter(ProviderAdapter, Generic[OptionsT]):
    """Own one HTTP connection whose protocol behaviour is declared by a provider profile."""

    connection_options = WireConnectionOptions
    protocol_version = 1
    default_base_url: str | None = None
    default_max_tokens: int | None = None
    default_thinking = False

    def __init__(self, cfg: ProviderConfig):
        self.profile = get_profile(cfg.kind)
        self.wire_options = WireConnectionOptions.model_validate(cfg.model_extra or {})
        self._minted_token: str | None = None
        self._minted_endpoint: str | None = None
        self._minted_at = 0.0
        self._credential: OAuthCredential | None = None
        self._credential_source: tuple[str, str | None] | None = None
        super().__init__(cfg)
        self.base_url = cfg.base_url or self.profile.base_url

    @classmethod
    def profile_for(cls, kind: str) -> ProviderProfile:
        """Return the profile this adapter class would use for a provider kind."""
        return get_profile(kind)

    @classmethod
    def validate_connection(cls, cfg: ProviderConfig) -> None:
        profile = get_profile(cfg.kind)
        cls.connection_options.model_validate(cfg.model_extra or {})
        endpoint = cls.endpoint(cfg)
        if profile.requires_base_url and not endpoint:
            raise ValueError(f"Provider {cfg.kind} requires base_url")
        if endpoint:
            ProviderConfig.validate_url(endpoint)

    @classmethod
    def endpoint(cls, cfg: ProviderConfig) -> str | None:
        """Resolve the same non-network endpoint for validation, preview and requests."""
        return configured_base_url(get_profile(cfg.kind), cfg.base_url)

    @classmethod
    def output_limit(
        cls,
        options: BaseModel,
        hint: int | None,
        explicit: int | None,
        *,
        profile: ProviderProfile | None = None,
    ) -> int | None:
        """Resolve the output cap; thinking requests need room for their reasoning tokens."""
        requested = getattr(options, "thinking", None)
        thinking = profile.default_thinking if profile is not None else cls.default_thinking
        if requested is not None:
            thinking = bool(requested)
        if profile is not None:
            # Anthropic supplies native thinking even without a profile builder.
            thinking = thinking and (
                profile.reasoning is not None or profile.wire == WIRE_ANTHROPIC
            )
        limit = explicit if explicit is not None else hint
        if limit is None:
            default = profile.default_max_tokens if profile is not None else None
            limit = default or cls.default_max_tokens
        if limit is None:
            return None
        if thinking and limit < MIN_THINKING_OUTPUT_TOKENS:
            if explicit is not None:
                raise ValueError(
                    "Thinking mode requires max_output_tokens >= "
                    f"{MIN_THINKING_OUTPUT_TOKENS}; disable thinking or increase the "
                    "explicit limit"
                )
            return MIN_THINKING_OUTPUT_TOKENS
        return limit

    def validate_credentials(self) -> None:
        """Fail fast with an actionable message when no usable credential exists."""
        self.auth_token()

    def auth_token(self) -> str:
        """Return a usable token, refreshing or minting it when the provider requires it."""
        return self.credential().access_token

    def credential(self) -> OAuthCredential:
        """Return a usable credential; subscription rotation uses the shared durable cache."""
        if self.profile.auth == AUTH_VERTEX:
            token, _ = self._vertex_credentials()
            return OAuthCredential(access_token=token)
        with self._client_lock:
            source = read_env_value(candidate_env_vars(self.profile, self.api_key_env))
            cached = self._credential if source == self._credential_source else None
            credential = resolve_credential(
                self.profile, api_key_env=self.api_key_env, cached=cached
            )
            self._credential, self._credential_source = credential, source
            return credential

    def _vertex_credentials(self) -> tuple[str, str]:
        """Mint and cache the Vertex AI token and its OpenAI-compatible endpoint."""
        with self._client_lock:
            if (
                self._minted_token is None
                or self._minted_endpoint is None
                or time.monotonic() - self._minted_at >= 300
            ):
                token, endpoint = vertex_token_and_base_url(self.profile, self.cfg.base_url)
                self._minted_token, self._minted_endpoint = token, endpoint
                self._minted_at = time.monotonic()
            return self._minted_token, self._minted_endpoint

    def resolve_base_url(self) -> str:
        """Resolve the endpoint for one request without trailing slash."""
        if self.profile.auth == AUTH_VERTEX:
            return self._vertex_credentials()[1].rstrip("/")
        resolved = self.endpoint(self.cfg)
        if not resolved:
            raise ValueError(f"Provider {self.cfg.kind} requires base_url")
        return resolved.rstrip("/")

    def auth_headers(self) -> dict[str, str]:
        """Build the credential headers this provider expects."""
        credential = self.credential()
        token = credential.access_token
        if self.profile.api_key_header == "x-api-key":
            headers = {"x-api-key": token}
        else:
            headers = {"authorization": f"Bearer {token}"}
        return {**headers, **self.credential_headers(credential)}

    def credential_headers(self, credential: OAuthCredential) -> dict[str, str]:
        """Return extra headers derived from the credential; most providers add none."""
        return {}

    def request_headers(self, *, accept: str = "application/json") -> dict[str, str]:
        """Merge profile, credential and protocol headers for one request."""
        return {
            "content-type": "application/json",
            **self.profile.default_headers,
            **self.profile.auth_headers,
            **self.auth_headers(),
            "accept": accept,
        }

    def reasoning_parts(self, model: str, options: ProviderOptions) -> tuple[dict, dict]:
        """Place reasoning controls through the profile's builder for one request."""
        builder = self.profile.reasoning
        if builder is None:
            return {}, {}
        request = ReasoningRequest(
            model=model,
            enabled=options.thinking_enabled(self.profile.default_thinking),
            effort=options.reasoning_effort or self.profile.default_effort,
        )
        return builder(request)

    def http_client(self) -> httpx.Client:
        """Create a client honouring the connection timeout; the caller closes it."""
        return httpx.Client(timeout=self.cfg.timeout)

    def post_json(
        self,
        path: str,
        body: Mapping[str, Any],
        *,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """POST one JSON request and return the decoded JSON object."""
        with self.open_stream(path, body, headers=headers, accept="application/json") as response:
            return self.json_object(response)

    @contextmanager
    def open_stream(
        self,
        path: str,
        body: Mapping[str, Any],
        *,
        headers: Mapping[str, str] | None = None,
        accept: str = EVENT_STREAM_TYPE,
    ) -> Iterator[httpx.Response]:
        """POST one request and yield the open response before any body is read.

        Credential and status errors are mapped before the body is handed over, so a caller
        only ever parses a response the provider accepted.
        """
        url = path if path.startswith("http") else f"{self.resolve_base_url()}{path}"
        with self.http_client() as client:
            with client.stream(
                "POST",
                url,
                json=dict(body),
                headers={**self.request_headers(accept=accept), **(headers or {})},
            ) as response:
                if response.status_code in {401, 403}:
                    raise credential_failure(
                        self.profile,
                        response.status_code,
                        response.read().decode(errors="replace"),
                    )
                if response.status_code >= 400:
                    raise status_failure(response, response.read().decode(errors="replace"))
                yield response

    def json_object(self, response: httpx.Response) -> dict[str, Any]:
        """Decode a JSON object response, rejecting anything else."""
        # Read a streamed body before decoding it; otherwise httpx reports ResponseNotRead and
        # hides the payload, including the provider's own error message.
        response.read()
        try:
            payload = response.json()
        except ValueError as error:
            raise RuntimeError(
                f"{self.profile.display()} returned a non-JSON response (HTTP {response.status_code})"
            ) from error
        if not isinstance(payload, dict):
            raise RuntimeError(f"{self.profile.display()} returned an unexpected JSON payload")
        return payload

    @abstractmethod
    def _request(
        self,
        messages: Messages,
        model: ResolvedModel[OptionsT],
        *,
        json_mode: bool,
        context: RequestContext,
    ) -> str:
        raise NotImplementedError


__all__ = [
    "EVENT_STREAM_TYPE",
    "MAX_ERROR_BODY_CHARS",
    "MIN_THINKING_OUTPUT_TOKENS",
    "PROFILE_WIRES",
    "WIRE_ADAPTERS",
    "ProfileAdapter",
    "credential_failure",
    "is_event_stream",
    "merge_extra_body",
]
