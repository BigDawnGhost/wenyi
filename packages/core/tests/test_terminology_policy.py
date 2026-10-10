import hashlib

import pytest
from wenyi_core.config import Config, PipelineConfig
from wenyi_core.i18n.policy.models import OperationBinding
from wenyi_core.i18n.policy.registry import OPERATIONS
from wenyi_core.i18n.policy.resolver import resolve_policy
from wenyi_core.i18n.resources import read_text
from wenyi_core.llm.operations import configured_operations


def config(source="en", target="zh", **flags):
    return Config(source_lang=source, target_lang=target, pipeline=PipelineConfig(**flags))


def test_defaults_and_removed_configuration():
    default = config()
    assert not default.pipeline.terminology_context
    assert not default.language_policy().enabled("terminology.context")
    assert not any(
        op.startswith("terminology.") for op in configured_operations(default, "prepare")
    )
    with pytest.raises(ValueError, match="kinship_resolution"):
        PipelineConfig(kinship_resolution=True)
    assert {op.id for op in OPERATIONS.values() if op.point == "evidence.prepare"} == {
        "terminology.context"
    }


@pytest.mark.parametrize(
    "source,target", [("en", "zh"), ("ja", "zh"), ("fr", "en"), ("auto", "ja")]
)
def test_terminology_is_language_independent_and_book_only(source, target):
    selected = config(source, target, terminology_context=True)
    for phase in ("analysis", "translation"):
        assert selected.language_policy(phase).enabled("terminology.context")
    for phase in ("review", "export"):
        assert not selected.language_policy(phase, format="epub").enabled("terminology.context")
    assert not selected.language_policy(path="srt").enabled("terminology.context")


def test_task_freezing_and_export_independence():
    default = config()
    selected = config(terminology_context=True)
    selected.freeze_language_policies("source")
    for phase in ("analysis", "translation"):
        plan = selected.language_policy(phase)
        for group in ("discover", "evidence", "merge"):
            for role in ("system", "user"):
                assert f"terminology_{group}_{role}" in dict(plan.templates)
        assert not any(
            name.startswith("terminology_") for name, _ in default.language_policy(phase).templates
        )
        assert plan.semantic_fingerprint != default.language_policy(phase).semantic_fingerprint
    assert (
        selected.language_policy("export", format="epub").fingerprint
        == default.language_policy("export", format="epub").fingerprint
    )
    assert not any(
        name.startswith("terminology_")
        for name, _ in selected.language_policy(path="srt").templates
    )
    for workflow in ("prepare", "translate"):
        assert {"terminology.discover", "terminology.evidence", "terminology.merge"} <= set(
            configured_operations(selected, workflow)
        )
    for workflow in ("srt", "review"):
        assert not any(
            op.startswith("terminology.") for op in configured_operations(selected, workflow)
        )


def test_terminology_templates_are_independent_of_relationship_guidance():
    templates = dict(config(terminology_context=True).language_policy().templates)
    for task, text in templates.items():
        if task.startswith("terminology_"):
            for excluded in (
                "$kinship",
                "kinship",
                "relationships",
                "mentions",
                "older_than",
                "identity_scope",
                "brother",
                "sister",
                "uncle",
                "cousin",
            ):
                assert excluded not in text, (task, excluded)
        if task in {"translator_user", "polisher_user", "polisher_continue_user"}:
            assert "$evidence_context" not in text
    for task in ("translator_user", "polisher_user"):
        assert "$glossary" in templates[task]
    for kind in ("evidence", "merge"):
        text = templates[f"terminology_{kind}_system"]
        assert all(
            field in text for field in ("note", "reading", "gender", "aliases", "evidence_refs")
        )
        assert "4000" in text and "256" in text
    assert "Aliases must always be empty" in templates["terminology_merge_system"]


def test_default_analysis_templates_and_fingerprints_match_feature_baseline():
    # Recorded from 11aaa491d2992a844aafdb7e8025a98417e24b0c, not the enhanced variants.
    expected = {
        "analyzer": (
            "1aec5dc08746223c88791be152ae20cc2129632f29eb695c16f93db8724efcfb",
            "39e8b63c236f161732ada47e4ca1d7541c8be857575434e29ab1fff9ce6d768d",
        ),
        "chapter_digest": (
            "47d2e5e75cd7a457ad2e0a59c31c55a6e6da6e65859e0b6c1f1f8deaa5ec8a93",
            "2347b767b1955282a5900183dc470114dbc0fac38c86be0d1914327c5f8d3754",
        ),
        "book_synopsis": (
            "2cb628580d1641336412be5627e180a7f4ba0d5efce971f2bab6e8f1c72252f0",
            "28a8783ff3bf8b8e09618f2330fb09a67e0ac84780e4325f1feb9585c54ebb8e",
        ),
    }
    plan = Config.from_dict({"llm": {"preset": "fake"}}).language_policy("analysis")
    for task, (template_hash, fingerprint) in expected.items():
        template = dict(plan.templates)[f"{task}_system"]
        assert hashlib.sha256(template.encode("utf-8")).hexdigest() == template_hash
        assert plan.task_fingerprint(task) == fingerprint


def test_selected_prescan_variants_freeze_actual_resource_paths():
    plan = config(terminology_context=True).language_policy("analysis")
    for task in ("analyzer", "chapter_digest", "book_synopsis"):
        path = f"tasks/{task}_terminology_system.txt"
        assert path in dict(plan.resources)
        assert f"tasks/{task}_system.txt" not in dict(plan.resources)
        assert dict(plan.templates)[f"{task}_system"] == read_text(path)


def test_explicitly_disabled_operation_does_not_select_feature_templates():
    selected = config(terminology_context=True).language_policy("analysis")
    plan = resolve_policy(selected.context, {"terminology.context": OperationBinding(mode="off")})
    assert not plan.enabled("terminology.context")
    assert plan.templates == config().language_policy("analysis").templates
