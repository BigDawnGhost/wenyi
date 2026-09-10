"""Explicit, offline conversion of retired configuration and usage formats."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .configuration import LLMConfig
from .operations import TIERS
from .registry import provider_spec


def convert_config(raw: dict[str, Any]) -> dict[str, Any]:
    """Convert one old config in memory; the normal loader never calls this function."""
    result = deepcopy(raw)
    old = result.get("llm") or {}
    if any(key in old for key in ("preset", "providers", "models", "routes")):
        raise ValueError("Configuration already uses model routing")
    allowed = {
        "provider",
        "base_url",
        "api_key_env",
        "reasoning_style",
        "timeout",
        "max_retries",
        "tiers",
    }
    if set(old) - allowed:
        raise ValueError(f"Unknown old LLM fields: {sorted(set(old) - allowed)}")
    kind = old.get("provider", "deepseek")
    if not isinstance(kind, str) or not kind.strip():
        raise ValueError("The old LLM provider must be a non-empty string")
    kind = {"google": "gemini", "orca-router": "orcarouter"}.get(kind, kind)
    provider = provider_spec(kind)
    factory = getattr(provider._module(), "preset_models", None)
    defaults = factory() if factory else {}
    supplied = old.get("tiers") or {}
    if set(supplied) - set(TIERS):
        raise ValueError("Unknown old tier name")
    definitions = {}
    for tier in TIERS:
        item = supplied.get(tier, {})
        if set(item) - {"model", "options"}:
            raise ValueError(f"Unknown fields in old tier {tier}")
        default = defaults.get(tier)
        model = item.get("model") or (default.model if default else None)
        if model:
            options = {
                **(default.options.model_dump() if default else {}),
                **item.get("options", {}),
            }
            cap = options.pop("max_output_tokens", None) if kind == "gemini" else None
            definitions[tier] = {"provider": "default", "model": model, "options": options}
            if cap is not None:
                definitions[tier]["max_output_tokens"] = cap
    if "strong" not in definitions:
        raise ValueError("The old configuration has no strong model")
    # Materialize the old fallback once, so the new runtime requires no compatibility path.
    definitions.setdefault("cheap", deepcopy(definitions["strong"]))
    definitions.setdefault("fast", deepcopy(definitions["cheap"]))
    connection = {
        key: value
        for key, value in old.items()
        if key not in {"provider", "tiers", "reasoning_style"} and value is not None
    }
    connection["kind"] = kind
    if "reasoning_style" in old and kind in {"openai-compatible", "ollama", "vllm", "orcarouter"}:
        connection["reasoning_style"] = old["reasoning_style"]
    llm = {
        "providers": {"default": connection},
        "models": definitions,
        "tiers": {tier: tier for tier in TIERS},
        "routes": {},
    }
    pipeline = result.get("pipeline") or {}
    review_tier = pipeline.pop("review_agent_tier", None)
    if review_tier is not None:
        llm["routes"] = {
            operation: {"tier": review_tier}
            for operation in ("review.verify", "review.arbitrate", "review.fix")
        }
    LLMConfig.model_validate(llm)
    result["llm"] = llm
    return result
