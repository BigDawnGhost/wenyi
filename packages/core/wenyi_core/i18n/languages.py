"""Built-in language registry, explicit aliases and source/target/pair rules."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from .genres import DEFAULT_GENRE, compose
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


def profile(code: str, *, genre: str = DEFAULT_GENRE) -> dict[str, Any]:
    """Resolve ancestors before children, replacing each operation binding in full."""
    language = require_language(code)
    return _profile(language, (), genre=genre)


def _profile(language: str, ancestors: tuple[str, ...], *, genre: str) -> dict[str, Any]:
    if language in ancestors:
        raise ValueError(
            "Language profile inheritance cycle: " + " -> ".join((*ancestors, language))
        )
    data = read_json(f"languages/{language}.json")
    data = {
        key: compose(value, genre=genre) if isinstance(value, str) else value
        for key, value in data.items()
    }
    if "extends" in data and (not isinstance(data["extends"], str) or not data["extends"].strip()):
        raise ValueError(f"Invalid parent in language profile {language}")
    parent = data.pop("extends", None)
    base = _profile(require_language(parent), (*ancestors, language), genre=genre) if parent else {}
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


def glossary_context_dependent_sources(code: str) -> frozenset[str]:
    """Return source forms whose translation depends on speaker and listener.

    Self-references and forms of address such as Chinese 孤 or 大人 have no stable
    book-wide rendering, so automatic glossary extraction must not store them.
    """
    if not normalize_language(code):
        return frozenset()
    values = profile(code).get("glossary_context_dependent_sources", [])
    return frozenset(value for value in values if isinstance(value, str) and value.strip())


def glossary_single_character_names_only(code: str) -> bool:
    """Return True when one-character sources are mostly words, not names (e.g. Chinese)."""
    return bool(normalize_language(code)) and (
        profile(code).get("glossary_single_character_names_only") is True
    )


@lru_cache(maxsize=64)
def glossary_generic_target_words(code: str) -> frozenset[str]:
    """Return target-language words that never identify one character or thing.

    A target made only of such words, like English "I", "your son" or "Your Excellency",
    is a pronoun or a bare title whose wording depends on who is speaking.
    """
    if not normalize_language(code):
        return frozenset()
    values = profile(code).get("glossary_generic_target_words", [])
    return frozenset(
        value.casefold() for value in values if isinstance(value, str) and value.strip()
    )


@lru_cache(maxsize=64)
def inline_gloss_markers(code: str) -> tuple[str, ...]:
    """Return the words a translator uses to open an inline note, like English "(lit. ...".

    A language without markers gets no inline-gloss alert: a plain parenthetical is ordinary
    prose in many styles, so only an explicit note counts as an inserted gloss.
    """
    if not normalize_language(code):
        return ()
    values = profile(code).get("inline_gloss_markers", [])
    return tuple(value.strip() for value in values if isinstance(value, str) and value.strip())


def label(code: str) -> str:
    return profile(code)["label"] if normalize_language(code) else "the source language"


def resolve_pair_key(src: str, tgt: str) -> str | None:
    """Resolve the best matching pair key in ``pairs/registry.json``, falling back to base tags."""
    norm_src = normalize_language(src)
    norm_tgt = require_language(tgt)
    pairs = read_json("pairs/registry.json")
    candidates = [f"{norm_src}__{norm_tgt}"]
    src_base = norm_src.split("-", 1)[0] if "-" in norm_src else norm_src
    tgt_base = norm_tgt.split("-", 1)[0] if "-" in norm_tgt else norm_tgt
    for pair in (
        f"{norm_src}__{tgt_base}",
        f"{src_base}__{norm_tgt}",
        f"{src_base}__{tgt_base}",
    ):
        if pair not in candidates:
            candidates.append(pair)
    for pair in candidates:
        if pair in pairs:
            return pair
    return None


def honorific_rule(src: str, tgt: str) -> str:
    """Select forms-of-address guidance from the language pair, not user configuration."""
    rule = read_json("shared/honorific.json")["rule"]
    pair = resolve_pair_key(src, tgt)
    if pair is not None:
        pairs = read_json("pairs/registry.json")
        rule = read_json(f"pairs/{pairs[pair]}").get("honorific", rule)
    return rule


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
