"""Built-in language registry, explicit aliases and source/target/pair rules."""

from __future__ import annotations

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


def profile(code: str) -> dict[str, str]:
    language = require_language(code)
    data = read_json(f"languages/{language}.json")
    parent = data.pop("extends", None)
    if parent:
        base = read_json(f"languages/{parent}.json")
        base.update(data)
        data = base
    return data


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
    saved_target = require_language(manifest.get("target_lang") or "zh")
    if saved_target != require_language(target):
        raise ValueError(
            f"Saved target language {saved_target} does not match requested {target}. "
            "Use the matching language.target or a separate paths.state_dir."
        )
    requested_source = require_language(source, allow_auto=True)
    saved_source = normalize_language(manifest.get("source_lang") or "")
    if requested_source != "auto" and saved_source and requested_source != saved_source:
        raise ValueError(
            f"Saved source language {saved_source} does not match requested {source}. "
            "Use the matching language.source or a separate paths.state_dir."
        )
