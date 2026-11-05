"""Opt-in live connectivity probes for configured model profiles."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, nullcontext
from dataclasses import asdict, dataclass, replace
from typing import Any

from .configuration import LLMConfig
from .credentials import validate_credential
from .errors import describe_provider_failure
from .operations import require_operation
from .registry import provider_spec
from .retrying import EmptyResponseError, TruncatedResponseError
from .routing import model_route, resolve_routes
from .transport import ProviderAdapter, RequestContext, ResolvedModel

DEFAULT_PROBE_TIMEOUT = 30.0
PROBE_OUTPUT_TOKENS = 16
PROBE_MESSAGES = ({"role": "user", "content": "Reply with the single word: OK"},)
JSON_PROBE_OUTPUT_TOKENS = 128
_JSON_PROBE_VALUE = {"probe": "wenyi", "items": [1, 2]}
_JSON_PROBE_TEXT = json.dumps(_JSON_PROBE_VALUE, sort_keys=True)
_JSON_PROBE_MESSAGES = (
    {
        "role": "user",
        "content": f"Return exactly this JSON object, without other text: {_JSON_PROBE_TEXT}",
    },
)
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
    json_mode: str | None = None
    json_category: str | None = None
    json_status_code: int | None = None

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
    detect_json: bool = False,
) -> list[ProbeResult]:
    """Probe every profile reachable by ``operations``, including explicit fallbacks."""
    return _probe_all(
        config,
        probe_targets(config, tuple(operations)),
        credentials=credentials,
        timeout=timeout,
        detect_json=detect_json,
    )


def probe_profiles(
    config: LLMConfig,
    profiles: Iterable[str] | None = None,
    *,
    credentials: Mapping[str, str | None] | None = None,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
    detect_json: bool = False,
) -> list[ProbeResult]:
    """Probe registered profiles (all by default), independent of any workflow."""
    selected = list(config.models) if profiles is None else list(dict.fromkeys(profiles))
    for profile in selected:
        if profile not in config.models:
            raise ValueError(f"Unknown model profile: {profile}")
    users = probe_targets(config, resolve_routes(config))
    return _probe_all(
        config,
        {profile: users.get(profile, ()) for profile in selected},
        credentials=credentials,
        timeout=timeout,
        detect_json=detect_json,
    )


def _probe_all(
    config: LLMConfig,
    targets: Mapping[str, tuple[str, ...]],
    *,
    credentials: Mapping[str, str | None] | None,
    timeout: float,
    detect_json: bool,
) -> list[ProbeResult]:
    """Probe profiles without retries; optional JSON negotiation shares each profile's deadline.

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
                lambda item: _probe(
                    config,
                    item[0],
                    item[1],
                    credentials=credentials,
                    timeout=timeout,
                    detect_json=detect_json,
                ),
                targets.items(),
            )
        )


def _probe(
    config: LLMConfig,
    profile: str,
    operations: tuple[str, ...],
    *,
    credentials: Mapping[str, str | None] | None,
    timeout: float,
    detect_json: bool,
) -> ProbeResult:
    operation = operations[0] if operations else _DEFAULT_PROBE_OPERATION
    route = model_route(config, operation, profile, origin="probe")
    connection = config.providers[route.provider]
    adapter_type = provider_spec(connection.kind).adapter_type()
    request_model = route.request_model()
    # Probe settings replace workflow retries and long generation timeouts for this call only.
    probe_config = connection.model_copy(
        update={"max_retries": 0, "timeout": min(timeout, connection.timeout)}
    )

    def make_adapter(remaining: float) -> ProviderAdapter:
        bounded = probe_config.model_copy(update={"timeout": min(remaining, probe_config.timeout)})
        return (
            adapter_type(bounded)
            if credentials is None
            else adapter_type(bounded, credentials=(credentials.get(route.provider),))
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

    context = RequestContext(
        operation=operation,
        tier="probe",
        # Respect provider rules such as the minimum output size for thinking modes.
        max_tokens=adapter_type.output_limit(request_model.options, PROBE_OUTPUT_TOKENS, None),
        emit=lambda event, **data: None,
        record_usage=lambda sample: None,
        attempt_scope=nullcontext,
        # Each candidate must be tested exactly as named, without hidden runtime fallback.
        allow_json_negotiation=False,
    )
    started = time.monotonic()
    with closing(make_adapter(timeout)) as adapter:
        try:
            adapter.validate_credentials()
        except RuntimeError as error:
            # Credential validation messages name the environment variable, never its value.
            return result(False, "missing_credential", str(error))
        try:
            adapter.generate(
                [dict(message) for message in PROBE_MESSAGES],
                request_model,
                json_mode=False,
                context=context,
            )
        except _REACHABLE_RESPONSES:
            connected = result(
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
        else:
            connected = result(True, "ok", "The provider replied.", latency_ms=_elapsed_ms(started))
        candidates = adapter.json_probe_candidates(request_model)
    if not detect_json:
        return connected
    return replace(
        _probe_json(make_adapter, candidates, context, connected, deadline=started + timeout),
        latency_ms=_elapsed_ms(started),
    )


def _probe_json(
    make_adapter: Callable[[float], ProviderAdapter],
    candidates: tuple[tuple[str, ResolvedModel], ...],
    context: RequestContext,
    connected: ProbeResult,
    *,
    deadline: float,
) -> ProbeResult:
    for index, (mode, model) in enumerate(candidates):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return replace(connected, json_category="timeout")
        with closing(make_adapter(remaining)) as adapter:
            try:
                text = adapter.generate(
                    [dict(message) for message in _JSON_PROBE_MESSAGES],
                    model,
                    json_mode=True,
                    context=replace(
                        context,
                        max_tokens=adapter.output_limit(
                            model.options, JSON_PROBE_OUTPUT_TOKENS, None
                        ),
                    ),
                )
                # No repairs, fences or loose equality (True == 1): require the complete receipt.
                if json.dumps(json.loads(text), sort_keys=True) != _JSON_PROBE_TEXT:
                    raise ValueError("Invalid JSON probe receipt")
            except (ValueError, *_REACHABLE_RESPONSES):
                return replace(connected, json_category="invalid_json_response")
            except Exception as error:  # noqa: BLE001 - Provider details never leave the probe.
                if index + 1 < len(candidates) and adapter.is_json_mode_unsupported(error):
                    continue
                failure = describe_provider_failure(error)
                return replace(
                    connected, json_category=failure.category, json_status_code=failure.status_code
                )
        return replace(connected, json_mode=mode, json_category="ok")
    return replace(connected, json_category="invalid_json_response")


def _elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)
