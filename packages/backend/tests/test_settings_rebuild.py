"""Global registries and project selections follow the same merge rules after upgrades."""

from copy import deepcopy

import pytest
from wenyi_backend.config_documents import (
    config_document,
    config_from_document,
    global_document,
    merge_project,
)
from wenyi_backend.config_rebuild import (
    rebuild_default_template,
    rebuild_settings,
    rebuild_strategy,
)
from wenyi_backend.global_settings import registered_models
from wenyi_core.config import Config


def test_valid_global_settings_are_not_rewritten():
    document = global_document(Config.from_dict({"llm": {"preset": "fake"}}))
    result = rebuild_settings(document)
    assert not result.changed
    assert result.document == document


@pytest.mark.parametrize(
    ("preset", "ref", "options", "expected"),
    [
        ("deepseek", "deepseek/deepseek-flash", {}, "high"),
        ("deepseek", "deepseek/deepseek-flash", {"reasoning_effort": "max"}, "max"),
        ("deepseek", "deepseek/deepseek-flash", {"thinking": False}, "off"),
        ("gemini", "gemini/gemini-3.6-flash", {"thinking_level": "LOW"}, "low"),
        ("gemini", "gemini/gemini-3.6-flash", {"thinking_budget": 4000}, "4000 tokens"),
        ("gemini", "gemini/gemini-3.6-flash", {}, None),
        ("fake", "fake/fake", {}, None),
    ],
)
def test_model_metadata_exposes_configured_thinking(preset, ref, options, expected):
    config = Config.from_dict({"llm": {"preset": preset, "models": {ref: {"options": options}}}})
    assert registered_models(config)[ref]["thinking_value"] == expected


def test_thinking_picker_excludes_levels_rejected_by_the_models_token_limit():
    ref = "deepseek/short"
    config = Config.from_dict(
        {
            "llm": {
                "preset": "deepseek",
                "models": {ref: {"max_output_tokens": 1024, "options": {"thinking": False}}},
            }
        }
    )
    assert registered_models(config)[ref]["thinking_levels"] == ["off"]


def test_global_settings_drop_removed_and_project_only_fields():
    original = {
        "llm": {"preset": "fake"},
        "pipeline": {
            "translation_mode": "three_draft_synthesis",
            "prompt_genre": "academic",
            "precision_concurrency": 1,
            "review": False,
        },
        "honorific": {"strategy": "normalize"},
    }
    result = rebuild_settings(original)
    assert result.document == {"llm": {"preset": "fake"}, "pipeline": {"review": False}}
    assert result.changed
    assert not rebuild_settings(result.document).changed


def test_project_rebuild_keeps_valid_custom_model_selections_and_genre():
    fake = Config.from_dict({"llm": {"preset": "fake"}})
    model = fake.llm.models["fake/fake"].model_dump(mode="json")
    defaults = Config.from_dict({"llm": {"preset": "fake", "models": {"fake/custom": model}}})
    original = {
        "llm": {
            "providers": {"private": {"kind": "fake"}},
            "tiers": {"strong": "custom", "cheap": "removed-model"},
            "routes": {"translation.body": {"model": "fake/custom"}},
            "budget": {"max_requests": 29},
        },
        "pipeline": {
            "translation_mode": "three_draft_synthesis",
            "polish": True,
            "prompt_genre": "academic",
            "precision_concurrency": 1,
        },
        "output": {"bilingual": True},
    }
    before = deepcopy(original)
    result = rebuild_settings(original, defaults=defaults, project=True)
    config = config_from_document(merge_project(config_document(defaults), result.document))
    assert "tiers" not in result.document["llm"]
    assert config.llm.routes["review.scan"] == defaults.llm.routes["review.scan"]
    assert config.llm.routes["translation.body"].model == "fake/custom"
    assert config.llm.budget.max_requests == 29
    assert config.pipeline.translation_mode == "three_draft_synthesis"
    assert config.prompt_genre == "academic"
    assert config.output.bilingual is True
    assert "providers" not in result.document["llm"]
    assert "precision_concurrency" not in result.document["pipeline"]
    assert original == before
    assert not rebuild_settings(result.document, defaults=defaults, project=True).changed


@pytest.mark.parametrize(
    "strategy",
    [
        {},
        {"template": "标准翻译", "steps": {"polish": False}},
        {"steps": {"review": False, "book_understanding": True}},
    ],
)
def test_valid_saved_workflow_selections_are_not_rewritten(strategy):
    before = deepcopy(strategy)
    result = rebuild_strategy(strategy)
    assert result.document == before
    assert not result.changed
    assert strategy == before


def test_saved_workflow_drops_retired_export_step_without_resetting_other_steps():
    original = {"steps": {"polish": False, "punctuation_normalize": True}}
    result = rebuild_strategy(original)
    assert result.document == {"steps": {"polish": False}}
    assert result.removed == ("strategy.steps.punctuation_normalize",)
    assert original["steps"]["punctuation_normalize"] is True
    assert not rebuild_strategy(result.document).changed


@pytest.mark.parametrize(
    "strategy",
    [
        {"template": "快速出稿"},
        {"steps": {"report": True}},
        {"steps": {"polish": "false"}},
        {"removed_field": True},
        [],
    ],
)
def test_invalid_saved_workflows_reset_without_compatibility_aliases(strategy):
    before = deepcopy(strategy)
    result = rebuild_strategy(strategy)
    assert result.document == {}
    assert result.removed == ("strategy",)
    assert strategy == before
    assert not rebuild_strategy(result.document).changed


@pytest.mark.parametrize("template", ["快速出稿", "missing", None, {}])
def test_unknown_default_templates_use_current_default(template):
    assert rebuild_default_template(template) == "标准翻译"
