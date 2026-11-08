"""Global registries and project selections follow the same merge rules after upgrades."""

from copy import deepcopy

from wenyi_backend.config_documents import (
    config_document,
    config_from_document,
    global_document,
    merge_project,
)
from wenyi_backend.config_rebuild import rebuild_settings
from wenyi_core.config import Config


def test_valid_global_settings_are_not_rewritten():
    document = global_document(Config.from_dict({"llm": {"preset": "fake"}}))
    result = rebuild_settings(document)
    assert not result.changed
    assert result.document == document


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
    model = fake.llm.models[fake.llm.tiers["strong"]].model_dump(mode="json")
    defaults = Config.from_dict({"llm": {"preset": "fake", "models": {"custom": model}}})
    original = {
        "llm": {
            "providers": {"private": {"kind": "fake"}},
            "tiers": {"strong": "custom", "cheap": "removed-model"},
            "routes": {"translation.body": {"model": "custom"}},
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
    assert config.llm.tiers["strong"] == "custom"
    assert config.llm.tiers["cheap"] == defaults.llm.tiers["cheap"]
    assert config.llm.routes["translation.body"].model == "custom"
    assert config.llm.budget.max_requests == 29
    assert config.pipeline.translation_mode == "three_draft_synthesis"
    assert config.prompt_genre == "academic"
    assert config.output.bilingual is True
    assert "providers" not in result.document["llm"]
    assert "precision_concurrency" not in result.document["pipeline"]
    assert original == before
    assert not rebuild_settings(result.document, defaults=defaults, project=True).changed
