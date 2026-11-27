"""Opt-in live connectivity probes for configured model profiles."""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from typing import Any

from .configuration import LLMConfig, ModelConfig, ProviderConfig
from .credentials import validate_credential
from .errors import describe_provider_failure
from .operations import require_operation
from .registry import provider_spec
from .retrying import EmptyResponseError, TruncatedResponseError
from .routing import ResolvedRoute, model_route, resolve_model, resolve_routes
from .transport import RequestContext

DEFAULT_PROBE_TIMEOUT = 30.0
PROBE_OUTPUT_TOKENS = 16
PROBE_MESSAGES = ({"role": "user", "content": "Reply with the single word: OK"},)
MAX_PROBE_WORKERS = 8
# Profiles outside every workflow route still need an operation to resolve output limits.
_DEFAULT_PROBE_OPERATION = "translation.body"
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
    """Probe every profile reachable by ``operations``, including explicit fallbacks."""
    return _probe_profiles(
        config,
        probe_targets(config, tuple(operations)),
        credentials=credentials,
        timeout=timeout,
    )


def probe_profiles(
    config: LLMConfig,
    profiles: Iterable[str] | None = None,
    *,
    credentials: Mapping[str, str | None] | None = None,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
) -> list[ProbeResult]:
    """Probe registered profiles (all by default), independent of any workflow."""
    selected = list(config.models) if profiles is None else list(dict.fromkeys(profiles))
    for profile in selected:
        if profile not in config.models:
            raise ValueError(f"Unknown model profile: {profile}")
    users = probe_targets(config, resolve_routes(config))
    return _probe_profiles(
        config,
        {profile: users.get(profile, ()) for profile in selected},
        credentials=credentials,
        timeout=timeout,
    )


def probe_draft_connection(
    connection_id: str,
    provider: ProviderConfig,
    models: Mapping[str, ModelConfig],
    *,
    credentials: Mapping[str, str | None] | None = None,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
) -> list[ProbeResult]:
    """Probe only the draft profiles on one connection, without saving or resolving a workflow."""
    provider_spec(provider.kind).validate_connection(provider)
    targets = [
        (
            resolve_model(
                _DEFAULT_PROBE_OPERATION,
                f"{connection_id}/{model_name}",
                provider,
                model,
                origin="probe",
            ),
            provider,
            (),
        )
        for model_name, model in models.items()
    ]
    return _probe_all(targets, credentials=credentials, timeout=timeout)


def _probe_profiles(
    config: LLMConfig,
    targets: Mapping[str, tuple[str, ...]],
    *,
    credentials: Mapping[str, str | None] | None,
    timeout: float,
) -> list[ProbeResult]:
    prepared = []
    for profile, operations in targets.items():
        operation = operations[0] if operations else _DEFAULT_PROBE_OPERATION
        route = model_route(config, operation, profile, origin="probe")
        prepared.append((route, config.providers[route.provider], operations))
    return _probe_all(prepared, credentials=credentials, timeout=timeout)


def _probe_all(
    targets: list[tuple[ResolvedRoute, ProviderConfig, tuple[str, ...]]],
    *,
    credentials: Mapping[str, str | None] | None,
    timeout: float,
) -> list[ProbeResult]:
    """Send one minimal, non-retried request per profile; results keep the input order.

    Each probe bills a few tokens. Usage is not recorded in any run ledger, and failures are
    reduced to the provider-independent categories from ``describe_provider_failure``.
    """
    if timeout <= 0:
        raise ValueError("Probe timeout must be positive")
    if credentials is not None:
        for secret in credentials.values():
            validate_credential(secret)
    if not targets:
        return []
    with ThreadPoolExecutor(max_workers=min(MAX_PROBE_WORKERS, len(targets))) as pool:
        # map() yields in submission order, so completion order never reorders results.
        return list(
            pool.map(
                lambda item: _probe(*item, credentials=credentials, timeout=timeout),
                targets,
            )
        )


def _probe(
    route: ResolvedRoute,
    connection: ProviderConfig,
    operations: tuple[str, ...],
    *,
    credentials: Mapping[str, str | None] | None,
    timeout: float,
) -> ProbeResult:
    adapter_type = provider_spec(route.provider_kind).adapter_type()
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
            route.profile,
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
        operation=route.operation,
        profile=route.profile,
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
