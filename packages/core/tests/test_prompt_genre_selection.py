"""Explicit genre selection composes the entire invocation without process-global state."""

from __future__ import annotations

from typing import cast

import pytest
from wenyi_core.agents.prompts import render_glossary
from wenyi_core.config import _DEFAULT_CONFIG_YAML, Config
from wenyi_core.glossary.store import GlossaryTerm
from wenyi_core.i18n import genres
from wenyi_core.i18n.genres import Genre
from wenyi_core.i18n.languages import profile
from wenyi_core.i18n.policy.models import PolicyContext
from wenyi_core.i18n.policy.resolver import resolve_policy
from wenyi_core.i18n.prompts import render
from wenyi_core.i18n.resources import read_json


def config(genre: str) -> Config:
    result = Config.from_dict({"language": {"source": "ja", "target": "en-GB"}})
    result.set_prompt_genre(genre)
    return result


def test_runtime_genre_is_absent_from_core_config_documents_and_schemas():
    academic = config("academic")
    document = academic.model_dump(mode="json")
    assert "prompt_genre" not in document
    assert "_prompt_genre" not in document
    assert "prompt_genre" not in document["pipeline"]
    assert "prompt_genre" not in _DEFAULT_CONFIG_YAML
    schema = Config.model_json_schema()
    assert "prompt_genre" not in schema["$defs"]["PipelineConfig"]["properties"]
    assert Config.model_validate(document).language_policy().context.genre == "literary"


def test_prompt_genre_is_not_a_yaml_configuration_field():
    with pytest.raises(ValueError, match="prompt_genre"):
        Config.from_dict({"pipeline": {"prompt_genre": "academic"}})


@pytest.mark.parametrize("genre", ["", None, "neutral", "Academic", "../academic"])
def test_invalid_runtime_genres_fail_before_model_calls(genre):
    with pytest.raises(ValueError, match="prompt genre"):
        config(genre)


@pytest.mark.parametrize("setter", ["set_prompt_genre", "set_experimental_prompt_genre"])
def test_genre_setters_clear_frozen_policies_and_validate_before_changes(setter):
    cfg = config("literary")
    cfg.freeze_language_policies()
    original = cfg.language_policy()
    select = getattr(cfg, setter)
    with pytest.raises(ValueError, match="prompt genre"):
        select("unknown")
    assert cfg.prompt_genre == "literary"
    assert cfg.language_policy() is original

    select("academic")
    assert cfg.prompt_genre == "academic"
    for phase in ("analysis", "translation", "review"):
        assert cfg.language_policy(phase).context.genre == "academic"
    assert cfg.language_policy().fingerprint != original.fingerprint


@pytest.mark.parametrize("phase", ["analysis", "translation", "review"])
def test_all_semantic_phases_select_academic_with_distinct_fingerprints(phase):
    academic = config("academic").language_policy(phase)
    literary = config("literary").language_policy(phase)
    assert academic.context.genre == "academic"
    assert academic.fingerprint != literary.fingerprint
    assert academic.semantic_fingerprint != literary.semantic_fingerprint
    assert "fiction" not in "\n".join(text for _, text in academic.templates)
    assert "fiction" not in "\n".join(value for _, value in academic.prompt_values)


def test_shared_and_inherited_language_rules_follow_the_selected_genre():
    plan = config("academic").language_policy()
    rules = dict(plan.prompt_values)
    assert "the author's stance, attribution and degree of certainty" in rules["target_guidance"]
    assert "British English" in rules["target_guidance"]
    assert "character voice" not in rules["metadata_guidance"]
    assert "every note, genre, tone, style guide." in rules["metadata_guidance"]
    assert "academic prose" in profile("en", genre="academic")["target_guidance"]
    assert "logical connectors" in profile("en", genre="academic")["target_guidance"]
    assert "authorial self-reference" in profile("ja", genre="academic")["target_guidance"]


def test_fallback_templates_follow_the_plan_genre_without_cross_invocation_leaks():
    # Review templates are not frozen into the analysis phase; the fallback still uses its genre.
    academic = config("academic").language_policy("analysis")
    literary = config("literary").language_policy("analysis")
    kwargs = {"src": "ja", "tgt": "en-GB", "max_evidence_rounds": 2}
    before = render("review_agent_system", plan=literary, **kwargs)
    assert "academic writing" in render("review_agent_system", plan=academic, **kwargs)
    assert render("review_agent_system", plan=literary, **kwargs) == before
    assert "academic writing" not in before


def test_academic_resources_are_frozen_before_later_resource_edits(monkeypatch):
    cfg = config("academic")
    cfg.freeze_language_policies()
    plan = cfg.language_policy()
    before = render("translator_system", src="ja", tgt="en-GB", plan=plan)

    def revised_json(path):
        data = read_json(path)
        if path == "genres/academic/fragments.json":
            data["genre_translation_style"] += " [resource revision]"
        return data

    monkeypatch.setattr(genres, "read_json", revised_json)
    assert render("translator_system", src="ja", tgt="en-GB", plan=plan) == before
    assert cfg.language_policy() is plan
    cfg.freeze_language_policies()
    assert cfg.language_policy().fingerprint != plan.fingerprint
    assert "[resource revision]" in render(
        "translator_system", src="ja", tgt="en-GB", plan=cfg.language_policy()
    )


def test_export_identity_does_not_depend_on_prompt_genre():
    literary = config("literary").language_policy("export", format="docx")
    academic = config("academic").language_policy("export", format="docx")
    assert academic.fingerprint == literary.fingerprint
    assert academic.export == literary.export


def test_unknown_policy_genre_is_rejected_explicitly():
    with pytest.raises(ValueError, match="prompt genre"):
        resolve_policy(PolicyContext("en", "zh", genre=cast(Genre, "unknown")))


def test_supplementary_glossary_guidance_uses_the_selected_genre():
    term = GlossaryTerm(source="Smith", target="史密斯", note="The author of the cited work.")
    literary = render_glossary([term], source_lang="en", note_source="Smith argues.")
    academic = render_glossary(
        [term], source_lang="en", note_source="Smith argues.", genre="academic"
    )
    assert "future identities or plot" in literary
    assert "future identities or plot" not in academic
    assert "The current source paragraph takes priority" in academic
    assert term.note in academic
