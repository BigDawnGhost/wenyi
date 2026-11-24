"""Deep-merge contract for layered LLM configuration sections."""

from __future__ import annotations

from wenyi_core.llm.configuration import merge_llm_settings


def test_flat_keys_inherit_base_fields_the_override_omits():
    base = {"preset": "standard", "budget": {"max_requests": 10}}
    assert merge_llm_settings(base, {"budget": {"max_requests": 99}}) == {
        "preset": "standard",
        "budget": {"max_requests": 99},
    }


def test_nested_dictionaries_merge_by_key_instead_of_replacement():
    base = {
        "providers": {
            "default": {
                "kind": "openai",
                "base_url": "https://api.example.com/v1",
                "timeout": 600,
            },
            "spare": {"kind": "openai"},
        }
    }
    override = {"providers": {"default": {"timeout": 30}}}
    merged = merge_llm_settings(base, override)
    assert merged["providers"]["spare"] == {"kind": "openai"}
    assert merged["providers"]["default"] == {
        "kind": "openai",
        "base_url": "https://api.example.com/v1",
        "timeout": 30,
    }


def test_routes_merge_per_operation_keeping_unspecified_route_fields():
    base = {
        "routes": {
            "review.scan": {"model": "a/slow"},
            "translation.body": {"model": "a/fast", "fallbacks": ["a/slow"]},
        }
    }
    override = {"routes": {"translation.body": {"model": "b/fast"}}}
    merged = merge_llm_settings(base, override)
    assert merged["routes"]["review.scan"] == {"model": "a/slow"}
    assert merged["routes"]["translation.body"] == {
        "model": "b/fast",
        "fallbacks": ["a/slow"],
    }


def test_lists_are_replaced_not_concatenated():
    base = {"routes": {"translation.body": {"model": "a/fast", "fallbacks": ["a/slow"]}}}
    override = {"routes": {"translation.body": {"fallbacks": ["b/cheap"]}}}
    merged = merge_llm_settings(base, override)
    assert merged["routes"]["translation.body"]["fallbacks"] == ["b/cheap"]
    assert merged["routes"]["translation.body"]["model"] == "a/fast"


def test_scalars_and_explicit_none_replace_base_values():
    base = {
        "models": {"a/fast": {"provider": "a", "model": "fast", "options": {"temperature": 0.2}}},
        "preset": "standard",
    }
    override = {
        "models": {"a/fast": {"options": {"temperature": None}}},
        "preset": None,
    }
    merged = merge_llm_settings(base, override)
    assert merged["models"]["a/fast"]["options"] == {"temperature": None}
    assert merged["models"]["a/fast"]["provider"] == "a"
    assert merged["preset"] is None


def test_merge_does_not_mutate_its_inputs():
    base = {"providers": {"default": {"timeout": 600}}}
    override = {"providers": {"default": {"timeout": 30}}}
    merge_llm_settings(base, override)
    assert base == {"providers": {"default": {"timeout": 600}}}
    assert override == {"providers": {"default": {"timeout": 30}}}


def test_empty_layers_are_noops():
    layer = {"preset": "standard"}
    assert merge_llm_settings({}, layer) == layer
    assert merge_llm_settings(layer, {}) == layer
