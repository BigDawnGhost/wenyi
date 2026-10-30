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


def test_shared_preparation_invalidates_old_sampled_task_identities():
    # Sampled preparation identities recorded from main at b7bf4c9 must not be reused.
    expected = {
        "analyzer": "c9b0f621c493c986b33ae18b21d753ed0d0e3b18ce7404cfd978cabf753b6cc1",
        "chapter_digest": "2347b767b1955282a5900183dc470114dbc0fac38c86be0d1914327c5f8d3754",
        "book_synopsis": "28a8783ff3bf8b8e09618f2330fb09a67e0ac84780e4325f1feb9585c54ebb8e",
    }
    plan = Config.from_dict({"llm": {"preset": "fake"}}).language_policy("analysis")
    for task, fingerprint in expected.items():
        assert plan.task_fingerprint(task) != fingerprint


@pytest.mark.parametrize("enabled", [False, True])
def test_shared_prescan_templates_freeze_actual_resource_paths(enabled):
    plan = config(terminology_context=enabled).language_policy("analysis")
    for task in ("analyzer", "chapter_digest", "book_synopsis"):
        path = f"tasks/{task}_system.txt"
        assert path in dict(plan.resources)
        assert f"tasks/{task}_terminology_system.txt" not in dict(plan.resources)
        assert dict(plan.templates)[f"{task}_system"] == read_text(path)


def test_default_polisher_preserves_original_resource_and_fingerprint():
    plan = Config.from_dict({"llm": {"preset": "fake"}}).language_policy("translation")
    path = "tasks/polisher_user.txt"
    assert path in dict(plan.resources)
    assert "tasks/polisher_terminology_user.txt" not in dict(plan.resources)
    assert dict(plan.templates)["polisher_user"] == read_text(path)
    assert "$source_context" not in dict(plan.templates)["polisher_user"]
    assert plan.task_fingerprint("polisher") == (
        "964a2d08eb651aea404703d04c6017a0f29655b03f35c93a5a373016bb2fc634"
    )


@pytest.mark.parametrize(
    "phase,fingerprint",
    [
        ("analysis", "395155e77e13b4da88691410724aaa83b5f924b9b8afb6690c196aed969af139"),
        ("translation", "d5b09a1db42684cc475fd55e88d84ec09ad2f0ab2ab79db91bd8c3dc19e303eb"),
        ("review", "5bef984bdb9767d863c622b98cb43cc2c35970a9e4d2bfbd4c613f3e5cfe59b1"),
        ("export", "f18cefb9111ecee5ca4a1470edf6351e51be6cd4983634e93aa8c64643f5b043"),
    ],
)
def test_only_default_analysis_phase_changes_from_feature_baseline(phase, fingerprint):
    # Recorded from unmodified main at b7bf4c9.
    selected = Config.from_dict({"llm": {"preset": "fake"}})
    plan = selected.language_policy(phase, format="epub" if phase == "export" else "")
    assert (plan.fingerprint != fingerprint) == (phase == "analysis")


def test_selected_polisher_variant_freezes_the_source_context_resource():
    plan = config(terminology_context=True).language_policy("translation")
    path = "tasks/polisher_terminology_user.txt"
    assert path in dict(plan.resources)
    assert "tasks/polisher_user.txt" not in dict(plan.resources)
    assert dict(plan.templates)["polisher_user"] == read_text(path)
    assert "$source_context" in dict(plan.templates)["polisher_user"]


def test_explicitly_disabled_operation_does_not_select_feature_templates():
    selected = config(terminology_context=True).language_policy("analysis")
    plan = resolve_policy(selected.context, {"terminology.context": OperationBinding(mode="off")})
    assert not plan.enabled("terminology.context")
    assert plan.templates == config().language_policy("analysis").templates
