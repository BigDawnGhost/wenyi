import pytest
from wenyi_core.config import Config, PipelineConfig
from wenyi_core.i18n.genres import compose
from wenyi_core.i18n.policy.models import content_hash
from wenyi_core.i18n.policy.registry import OPERATIONS
from wenyi_core.i18n.resources import read_text
from wenyi_core.llm.operations import configured_operations


def config(source="en", target="zh", **flags):
    return Config(source_lang=source, target_lang=target, pipeline=PipelineConfig(**flags))


def test_retired_experiment_and_default_config_compatibility():
    assert PipelineConfig.model_validate({"terminology_context": False}) == PipelineConfig()
    with pytest.raises(ValueError, match="has been removed"):
        PipelineConfig.model_validate({"terminology_context": True})
    assert "terminology.context" not in OPERATIONS
    for enabled in (0, 1):
        selected = config(glossary_note_workers=enabled)
        assert not any(
            op.startswith("terminology.") for op in configured_operations(selected, "translate")
        )
        assert ("glossary.note" in configured_operations(selected, "translate")) == bool(enabled)


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
    plan = config(glossary_note_workers=int(enabled)).language_policy("analysis")
    for task in ("analyzer", "chapter_digest", "book_synopsis"):
        path = f"tasks/{task}_system.txt"
        assert path in dict(plan.resources)
        assert f"tasks/{task}_terminology_system.txt" not in dict(plan.resources)
        expected = compose(read_text(path))
        assert dict(plan.templates)[f"{task}_system"] == expected
        assert dict(plan.resources)[path] == content_hash(expected)


def test_default_polisher_preserves_resource_and_invalidates_old_language_rules():
    plan = Config.from_dict({"llm": {"preset": "fake"}}).language_policy("translation")
    path = "tasks/polisher_user.txt"
    assert path in dict(plan.resources)
    assert "tasks/polisher_terminology_user.txt" not in dict(plan.resources)
    expected = compose(read_text(path))
    assert dict(plan.templates)["polisher_user"] == expected
    assert dict(plan.resources)[path] == content_hash(expected)
    assert "$source_context" not in dict(plan.templates)["polisher_user"]
    # The shared target-language requirement intentionally invalidates the old prompt identity.
    assert plan.task_fingerprint("polisher") != (
        "964a2d08eb651aea404703d04c6017a0f29655b03f35c93a5a373016bb2fc634"
    )


@pytest.mark.parametrize(
    "phase,fingerprint",
    [
        ("analysis", "395155e77e13b4da88691410724aaa83b5f924b9b8afb6690c196aed969af139"),
        ("translation", "d5b09a1db42684cc475fd55e88d84ec09ad2f0ab2ab79db91bd8c3dc19e303eb"),
        ("review", "6297d2547a9a32893e34291f22051e3d3dfda78fefa80ff3c5b784e938139611"),
    ],
)
def test_model_phases_reject_older_prompt_baselines(phase, fingerprint):
    # Older preparation rules changed analysis; the output rule changes translation and Fixer.
    selected = Config.from_dict({"llm": {"preset": "fake"}, "output": {"about_page": False}})
    assert selected.language_policy(phase).fingerprint != fingerprint


def test_export_identity_retires_language_filtered_source_markup():
    selected = Config.from_dict({"llm": {"preset": "fake"}, "output": {"about_page": False}})
    plan = selected.language_policy("export", format="epub")
    assert "preserve_source_ruby" not in plan.to_dict()["export"]
    assert plan.fingerprint != "d4de743d62c14ff316fee9bcb27075cae1f7f260a3cf9cf056914ed022d47fe6"
