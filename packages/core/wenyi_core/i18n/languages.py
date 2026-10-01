"""Built-in language registry, explicit aliases and source/target/pair rules."""

from __future__ import annotations

from typing import Any

from .resources import read_json


def supported_languages() -> tuple[str, ...]:
    return tuple(read_json("languages/registry.json")["languages"])


def normalize_language(code: str) -> str:
    """Return a supported code, or empty for unknown/auto; never truncate tags."""
    normalized = (code or "").strip().lower().replace("_", "-")
    registry = read_json("languages/registry.json")
    aliases = registry["aliases"]
    return aliases.get(normalized, "")


def require_language(code: str, *, allow_auto: bool = False) -> str:
    """Validate explicit configuration while preserving registered scripts and regions."""
    if allow_auto and (not code or code.strip().lower() == "auto"):
        return "auto"
    language = normalize_language(code)
    if not language:
        raise ValueError(
            f"Unsupported language: {code!r}; choose: {' / '.join(supported_languages())}"
        )
    return language


def profile(code: str) -> dict[str, Any]:
    """Resolve ancestors before children, replacing each operation binding in full."""
    language = require_language(code)
    return _profile(language, ())


def _profile(language: str, ancestors: tuple[str, ...]) -> dict[str, Any]:
    if language in ancestors:
        raise ValueError(
            "Language profile inheritance cycle: " + " -> ".join((*ancestors, language))
        )
    data = read_json(f"languages/{language}.json")
    if "extends" in data and (not isinstance(data["extends"], str) or not data["extends"].strip()):
        raise ValueError(f"Invalid parent in language profile {language}")
    parent = data.pop("extends", None)
    base = _profile(require_language(parent), (*ancestors, language)) if parent else {}
    policy = data.pop("policy", {})
    from .policy.models import ProfilePolicy

    policy = ProfilePolicy.model_validate(policy).model_dump(exclude_unset=True)
    inherited = base.pop("policy", {})
    origins = base.pop("_policy_origins", {})
    for key, value in policy.items():
        if not isinstance(value, dict) or any(item is None for item in value.values()):
            raise ValueError(f"Invalid {key} policy in language profile {language}")
        inherited[key] = {**inherited.get(key, {}), **value}
        origins[key] = {**origins.get(key, {}), **{operation: language for operation in value}}
    return {**base, **data, "policy": inherited, "_policy_origins": origins}


def label(code: str) -> str:
    return profile(code)["label"] if normalize_language(code) else "the source language"


def honorific_rule(strategy: str, src: str = "ja", tgt: str = "zh") -> str:
    rules = read_json("shared/honorific.json")
    pair = f"{normalize_language(src)}__{require_language(tgt)}"
    pairs = read_json("pairs/registry.json")
    if pair in pairs:
        rules.update(read_json(f"pairs/{pair}.json").get("honorific", {}))
    return rules.get(strategy, rules["keep_style"])


def translate_guidance(src: str, honorific_strategy: str = "keep_style", tgt: str = "zh") -> str:
    common = read_json("shared/guidance.json")
    source = profile(src)["source_guidance"] if normalize_language(src) else common["source"]
    target = profile(tgt)["target_guidance"]
    return "\n".join(
        (source, target, common["evidence"], honorific_rule(honorific_strategy, src, tgt))
    )


def term_guidance(src: str) -> str:
    common = read_json("shared/guidance.json")
    reading = profile(src)["term_guidance"] if normalize_language(src) else common["reading"]
    return reading + common["evidence"]


def validate_run_languages(manifest: dict, source: str, target: str) -> None:
    """Reject resuming an existing translation in a different language direction."""
    if any(
        not isinstance(manifest.get(key), str) or not manifest[key].strip()
        for key in ("source_lang", "target_lang")
    ):
        raise ValueError("State is missing source_lang or target_lang; create a new translation.")
    saved_target = require_language(manifest["target_lang"])
    if saved_target != require_language(target):
        raise ValueError(
            f"Saved target language {saved_target} does not match requested {target}. "
            "Use the matching language.target or a separate paths.state_dir."
        )
    requested_source = require_language(source, allow_auto=True)
    saved_source = require_language(manifest["source_lang"], allow_auto=True)
    if requested_source != "auto" and saved_source != "auto" and requested_source != saved_source:
        raise ValueError(
            f"Saved source language {saved_source} does not match requested {source}. "
            "Use the matching language.source or a separate paths.state_dir."
        )
