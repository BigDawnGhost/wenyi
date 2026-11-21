"""Explicit routing graphs for transport tests with injected SDK responses."""

from wenyi_core.llm.configuration import LLMConfig
from wenyi_core.llm.operation_names import OPERATION_NAMES
from wenyi_core.llm.registry import provider_spec


def routes_for(model, overrides=None):
    return {**{op: {"model": model} for op in OPERATION_NAMES}, **(overrides or {})}


def model_config(*, kind="deepseek", profiles=None, routes=None, **connection):
    factory = getattr(provider_spec(kind)._module(), "preset_model", None)
    default = factory() if factory else None
    models = {}
    refs = {}
    for name, profile in {"default": {}, **(profiles or {})}.items():
        remote = profile.get("model") or (default.model if default else "test-model")
        refs[name] = f"default/{remote}"
        models[refs[name]] = {
            "options": profile.get("options", default.options.model_dump() if default else {}),
        }
    return LLMConfig.model_validate(
        {
            "providers": {"default": {"kind": kind, **connection}},
            "models": models,
            "routes": routes_for(
                refs["default"],
                {op: {"model": refs[route["model"]]} for op, route in (routes or {}).items()},
            ),
        }
    )
