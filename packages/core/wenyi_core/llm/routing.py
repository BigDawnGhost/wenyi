"""Pure resolution of registered operations into immutable inference identities."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Any

from .configuration import LLMConfig
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
    tier: str | None
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
    tier: str | None = None,
    fallbacks: tuple[str, ...] = (),
    output_hint: int | None = None,
) -> ResolvedRoute:
    spec = require_operation(operation)
    model = config.models[profile]
    connection = config.providers[model.provider]
    provider = provider_spec(connection.kind)
    adapter = provider.adapter_type()
    options = provider.validate_model(model)
    if output_hint is not None and output_hint <= 0:
        raise ValueError("max_tokens must be positive")
    limit = adapter.output_limit(
        options, output_hint or spec.output_tokens, model.max_output_tokens
    )
    endpoint = connection.base_url or adapter.default_base_url
    connection_options = adapter.connection_options.model_validate(connection.model_extra or {})
    physical_provider = {
        "kind": connection.kind,
        "endpoint": endpoint.rstrip("/") if endpoint else None,
    }
    physical_model = {
        "provider": physical_provider,
        "model": model.model,
        "adapter_protocol": adapter.protocol_version,
        "options": options.model_dump(mode="json"),
        "max_output_tokens": limit,
        "connection_options": connection_options.model_dump(mode="json"),
    }
    return ResolvedRoute(
        operation,
        origin,
        tier,
        profile,
        model.provider,
        connection.kind,
        endpoint,
        model.model,
        canonical_json(options.model_dump(mode="json")),
        limit,
        identity(physical_provider),
        identity(physical_model),
        identity(
            {"operation": operation, "protocol": spec.protocol_version, "inference": physical_model}
        ),
        fallbacks,
    )


def resolve_routes(config: LLMConfig) -> Mapping[str, ResolvedRoute]:
    """Compile all registered routes without credentials, SDK construction or network I/O."""
    resolved: dict[str, ResolvedRoute] = {}

    def resolve(operation: str) -> ResolvedRoute:
        if operation in resolved:
            return resolved[operation]
        spec = require_operation(operation)
        route = config.routes.get(operation)
        if route is not None:
            tier = route.tier
            profile = route.model if route.model is not None else config.tiers[tier or "strong"]
            origin = f"llm.routes.{operation}"
            fallbacks = tuple(route.fallbacks)
        elif spec.inherits:
            parent = resolve(spec.inherits)
            tier, profile, fallbacks = parent.tier, parent.profile, parent.fallbacks
            origin = f"inherits {spec.inherits}"
        else:
            tier = spec.tier
            profile = config.tiers[tier or "strong"]
            fallbacks = ()
            origin = f"default tier {tier}"
        if fallbacks and spec.resumable_conversation:
            raise ValueError(
                f"{operation}: model failover is not allowed inside resumable evidence conversations"
            )
        result = model_route(
            config, operation, profile, origin=origin, tier=tier, fallbacks=fallbacks
        )
        resolved[operation] = result
        return result

    for operation in OPERATIONS:
        resolve(operation)
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
