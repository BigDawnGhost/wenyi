"""Opt-in live connectivity probes for configured model profiles."""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from typing import Any

from .configuration import LLMConfig
from .credentials import validate_credential
from .errors import describe_provider_failure
from .operations import require_operation
from .registry import provider_spec
from .retrying import EmptyResponseError, TruncatedResponseError
from .routing import model_route, resolve_routes
from .transport import RequestContext

DEFAULT_PROBE_TIMEOUT = 30.0
PROBE_OUTPUT_TOKENS = 16
PROBE_MESSAGES = ({"role": "user", "content": "Reply with the single word: OK"},)
# A cut-off or blank answer still proves that endpoint, credential and model accepted the call.
_REACHABLE_RESPONSES = (TruncatedResponseError, EmptyResponseError)


@dataclass(frozen=True)
class ProbeResult:
    """One profile's outcome; messages are fixed text and never echo provider responses."""

    profile: str
    connection: str
    provider: str
    model: str
    endpoint: str | None
    operations: tuple[str, ...]
    ok: bool
    category: str
    message: str
    status_code: int | None = None
    latency_ms: int | None = None

    def describe(self) -> dict[str, Any]:
        result = asdict(self)
        result["operations"] = list(self.operations)
        return result


def probe_targets(config: LLMConfig, operations: Iterable[str]) -> dict[str, tuple[str, ...]]:
    """Map each reachable profile, including explicit fallbacks, to the operations using it."""
    routes = resolve_routes(config)
    targets: dict[str, list[str]] = {}
    for operation in operations:
        require_operation(operation)
        route = routes[operation]
        for profile in (route.profile, *route.fallbacks):
            users = targets.setdefault(profile, [])
            if operation not in users:
                users.append(operation)
    return {profile: tuple(users) for profile, users in targets.items()}


def probe_connections(
    config: LLMConfig,
    operations: Iterable[str],
    *,
    credentials: Mapping[str, str | None] | None = None,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
) -> list[ProbeResult]:
    """Send one minimal, non-retried request per reachable profile in stable order.

    Each probe bills a few tokens. Usage is not recorded in any run ledger, and failures are
    reduced to the provider-independent categories from ``describe_provider_failure``.
    """
    if timeout <= 0:
        raise ValueError("Probe timeout must be positive")
    if credentials is not None:
        for secret in credentials.values():
            validate_credential(secret)
    operations = tuple(operations)
    return [
        _probe(config, profile, users, credentials=credentials, timeout=timeout)
        for profile, users in probe_targets(config, operations).items()
    ]


def _probe(
    config: LLMConfig,
    profile: str,
    operations: tuple[str, ...],
    *,
    credentials: Mapping[str, str | None] | None,
    timeout: float,
) -> ProbeResult:
    route = model_route(config, operations[0], profile, origin="probe")
    connection = config.providers[route.provider]
    adapter_type = provider_spec(connection.kind).adapter_type()
    request_model = route.request_model()
    # Probe settings replace workflow retries and long generation timeouts for this call only.
    probe_config = connection.model_copy(
        update={"max_retries": 0, "timeout": min(timeout, connection.timeout)}
    )
    adapter = (
        adapter_type(probe_config)
        if credentials is None
        else adapter_type(probe_config, credentials=(credentials.get(route.provider),))
    )

    def result(ok: bool, category: str, message: str, **extra: Any) -> ProbeResult:
        return ProbeResult(
            profile,
            route.provider,
            route.provider_kind,
            route.model,
            route.endpoint,
            operations,
            ok,
            category,
            message,
            **extra,
        )

    try:
        adapter.validate_credentials()
    except RuntimeError as error:
        # Credential validation messages name the environment variable, never its value.
        return result(False, "missing_credential", str(error))

    context = RequestContext(
        operation=operations[0],
        tier="probe",
        # Respect provider rules such as the minimum output size for thinking modes.
        max_tokens=adapter_type.output_limit(request_model.options, PROBE_OUTPUT_TOKENS, None),
        emit=lambda event, **data: None,
        record_usage=lambda sample: None,
        attempt_scope=nullcontext,
    )
    started = time.monotonic()
    try:
        adapter.generate(
            [dict(message) for message in PROBE_MESSAGES],
            request_model,
            json_mode=False,
            context=context,
        )
    except _REACHABLE_RESPONSES:
        return result(
            True,
            "reachable",
            "The provider accepted the request; the reply was empty or truncated.",
            latency_ms=_elapsed_ms(started),
        )
    except Exception as error:  # noqa: BLE001 - Every failure becomes a safe probe result.
        failure = describe_provider_failure(error)
        return result(
            False,
            failure.category,
            failure.message,
            status_code=failure.status_code,
            latency_ms=_elapsed_ms(started),
        )
    return result(True, "ok", "The provider replied.", latency_ms=_elapsed_ms(started))


def _elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)
