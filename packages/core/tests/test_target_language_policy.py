"""Shared target-language requirements without script-based rejection or data rewrites."""

import pytest
from wenyi_core.config import Config
from wenyi_core.i18n.languages import profile
from wenyi_core.i18n.policy.models import Phase
from wenyi_core.i18n.prompts import render
from wenyi_core.i18n.resources import read_text


@pytest.mark.parametrize("source,target", [("fr", "zh"), ("fr", "en"), ("en", "ja")])
@pytest.mark.parametrize("genre", ["literary", "academic"])
@pytest.mark.parametrize(
    "task,phase",
    [
        ("translator_system", "translation"),
        ("precision_generation_system", "translation"),
        ("polisher_system", "translation"),
        ("polisher_continue_user", "translation"),
        ("title_translator_system", "translation"),
        ("review_fixer_system", "review"),
    ],
)
def test_generated_text_keeps_complete_output_rules_in_every_stage(
    task, phase, source, target, genre
):
    config = Config.from_dict(
        {
            "language": {"source": source, "target": target},
            "pipeline": {
                "terminology_context": True,
                "translation_mode": (
                    "three_draft_synthesis" if task == "precision_generation_system" else "standard"
                ),
            },
        }
    )
    config.set_prompt_genre(genre)
    config.freeze_language_policies()
    prompt = render(
        task,
        src=source,
        tgt=target,
        plan=config.language_policy(phase),
        n=1,
        next_source="",
    )
    assert profile(target)["label"] in prompt
    assert read_text("shared/target_language.txt").strip() in prompt
    assert "For foreign-language wording embedded in the source" in prompt
    assert "original wording followed by its target-language translation in parentheses" in prompt
    assert "does not make ordinary source text or names bilingual" in prompt


@pytest.mark.parametrize(
    "task",
    [
        "analyzer_system",
        "chapter_digest_system",
        "book_synopsis_system",
        "glossary_extractor_system",
        "glossary_history_system",
        "terminology_discover_system",
        "reviewer_system",
        "review_agent_system",
        "review_arbiter_system",
    ],
)
def test_output_rule_does_not_change_preparation_extraction_or_review_decisions(task):
    prompt = render(task, src="fr", tgt="zh", max_evidence_rounds=2)
    assert read_text("shared/target_language.txt").strip() not in prompt


def test_source_identity_references_remain_original_not_target_renderings():
    analyzer = render("analyzer_system", src="fr", tgt="zh")
    assert "Preserve source exactly in its original language" in analyzer
    assert "do not propose translated names" in analyzer
    digest = render("chapter_digest_system", src="fr", tgt="zh")
    assert "quote character and terminology identities in their original source spelling" in digest
    synopsis = render("book_synopsis_system", src="fr", tgt="zh")
    assert "Keep quoted original source spellings" in synopsis


@pytest.mark.parametrize("mode", ["standard", "three_draft_synthesis"])
def test_shared_rule_revision_is_frozen_and_only_invalidates_consuming_tasks(monkeypatch, mode):
    values = {
        "language": {"source": "fr", "target": "zh"},
        "pipeline": {"translation_mode": mode, "terminology_context": True},
        "output": {"about_page": False},
    }
    original = Config.from_dict(values)
    original.freeze_language_policies()
    phases: tuple[Phase, ...] = ("analysis", "translation", "review")
    before = {phase: original.language_policy(phase) for phase in phases}
    marker = "Apply the revised target-language requirement."
    monkeypatch.setattr(
        "wenyi_core.i18n.policy.resolver.read_text",
        lambda path: read_text(path) + (marker if path == "shared/target_language.txt" else ""),
    )
    revised = Config.from_dict(values)
    tasks = {
        "analysis": "analyzer_system",
        "translation": (
            "precision_generation_system"
            if mode == "three_draft_synthesis"
            else "translator_system"
        ),
        "review": "review_fixer_system",
    }
    for phase in phases:
        plan = before[phase]
        assert original.language_policy(phase) == plan
        updated = revised.language_policy(phase)
        assert (updated.semantic_fingerprint != plan.semantic_fingerprint) == (phase != "analysis")
        prompt = render(tasks[phase], src="fr", tgt="zh", plan=updated)
        assert (marker in prompt) == (phase != "analysis")
    for task in ("analyzer", "chapter_digest", "book_synopsis"):
        assert revised.language_policy("analysis").task_fingerprint(task) == before[
            "analysis"
        ].task_fingerprint(task)
    for task in ("glossary_extractor", "glossary_history", "terminology_discover"):
        assert revised.language_policy().task_fingerprint(task) == before[
            "translation"
        ].task_fingerprint(task)
    assert (
        revised.language_policy("export", format="epub").fingerprint
        == original.language_policy("export", format="epub").fingerprint
    )
