"""Pure resolution of registered operations into immutable inference identities."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Any

from .configuration import LLMConfig, _located_error, split_model_ref
from .operations import OPERATIONS, require_operation
from .registry import provider_spec
from .transport import ResolvedModel


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def identity(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ResolvedRoute:
    operation: str
    origin: str
    profile: str
    provider: str
    provider_kind: str
    endpoint: str | None
    model: str
    options_json: str
    max_output_tokens: int | None
    provider_identity: str
    model_identity: str
    fingerprint: str
    fallbacks: tuple[str, ...] = ()

    def request_model(self) -> ResolvedModel:
        options = (
            provider_spec(self.provider_kind).options_type().model_validate_json(self.options_json)
        )
        return ResolvedModel(self.model, options)

    def describe(self) -> dict[str, Any]:
        result = asdict(self)
        result["options"] = json.loads(result.pop("options_json"))
        return result


def model_route(
    config: LLMConfig,
    operation: str,
    profile: str,
    *,
    origin: str,
    fallbacks: tuple[str, ...] = (),
    output_hint: int | None = None,
    thinking: str | None = None,
) -> ResolvedRoute:
    spec = require_operation(operation)
    model = config.models[profile]
    connection_name, remote_model = split_model_ref(profile)
    connection = config.providers[connection_name]
    provider = provider_spec(connection.kind)
    adapter = provider.adapter_type()
    overrides = None
    if thinking is not None:
        control = adapter.route_thinking(connection, remote_model)
        if control is None:
            raise ValueError("The selected model does not support a thinking override")
        overrides = control.apply(model.options, thinking)
    options = provider.validate_model(model, options=overrides)
    if output_hint is not None and output_hint <= 0:
        raise ValueError("max_tokens must be positive")
    limit = adapter.output_limit(options, output_hint, model.max_output_tokens)
    endpoint = connection.base_url or adapter.default_base_url
    connection_options = adapter.connection_options.model_validate(connection.model_extra or {})
    physical_provider = {
        "kind": connection.kind,
        "endpoint": endpoint.rstrip("/") if endpoint else None,
    }
    physical_model = {
        "provider": physical_provider,
        "model": remote_model,
        "adapter_protocol": adapter.protocol_version,
        "options": options.model_dump(mode="json"),
        "max_output_tokens": limit,
        "connection_options": connection_options.model_dump(mode="json"),
    }
    return ResolvedRoute(
        operation,
        origin,
        profile,
        connection_name,
        connection.kind,
        endpoint,
        remote_model,
        canonical_json(options.model_dump(mode="json")),
        limit,
        identity(physical_provider),
        identity(physical_model),
        identity(
            {"operation": operation, "protocol": spec.protocol_version, "inference": physical_model}
        ),
        fallbacks,
    )


def operation_route(
    config: LLMConfig, operation: str, *, output_hint: int | None = None
) -> ResolvedRoute:
    route = config.routes[operation]
    return model_route(
        config,
        operation,
        route.model,
        origin=f"llm.routes.{operation}",
        fallbacks=tuple(route.fallbacks),
        output_hint=output_hint,
        thinking=route.thinking,
    )


def resolve_routes(config: LLMConfig) -> Mapping[str, ResolvedRoute]:
    """Compile all registered routes without credentials, SDK construction or network I/O."""
    resolved: dict[str, ResolvedRoute] = {}
    for operation, spec in OPERATIONS.items():
        route = config.routes[operation]
        fallbacks = tuple(route.fallbacks)
        if fallbacks and spec.resumable_conversation:
            raise ValueError(
                f"{operation}: model failover is not allowed inside resumable evidence conversations"
            )
        try:
            resolved[operation] = operation_route(config, operation)
        except ValueError as error:
            if route.thinking is None:
                raise
            raise _located_error(
                ValueError(str(error)), ("routes", operation, "thinking")
            ) from None
    return MappingProxyType(resolved)


def inference_snapshot(config: LLMConfig, operations: Iterable[str]) -> dict[str, Any]:
    routes = resolve_routes(config)
    return {
        operation: {
            "primary": routes[operation].fingerprint,
            "fallbacks": [
                model_route(config, operation, profile, origin="fallback").fingerprint
                for profile in routes[operation].fallbacks
            ],
        }
        for operation in sorted(operations)
    }
