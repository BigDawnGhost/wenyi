"""Explicit routing graphs for transport tests with injected SDK responses."""

from trans_novel.llm.configuration import LLMConfig
from trans_novel.llm.registry import provider_spec


def model_config(*, kind="deepseek", profiles=None, **connection):
    factory = getattr(provider_spec(kind)._module(), "preset_models", None)
    defaults = factory() if factory else {}
    profiles = profiles or {}
    models = {}
    for tier in ("strong", "cheap", "fast"):
        default = defaults.get(tier)
        profile = profiles.get(tier, {})
        models[tier] = {
            "provider": "default",
            "model": profile.get("model") or (default.model if default else "test-model"),
            "options": profile.get("options", default.options.model_dump() if default else {}),
        }
    return LLMConfig.model_validate(
        {
            "providers": {"default": {"kind": kind, **connection}},
            "models": models,
            "tiers": {tier: tier for tier in models},
        }
    )
