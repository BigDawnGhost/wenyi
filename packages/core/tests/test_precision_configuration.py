"""Precision mode configuration, routing and language-policy contracts."""

from dataclasses import replace
from unittest.mock import patch

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner
from wenyi_cli.cli import app
from wenyi_core.config import Config, PipelineConfig
from wenyi_core.i18n.prompts import render
from wenyi_core.llm.operations import OPERATIONS, configured_operations, workflow_operations
from wenyi_core.llm.routing import resolve_routes

REMOVED_OPERATIONS = {"translation.select", "translation.verify", "translation.refine"}


def test_initial_draft_concurrency_is_not_a_configurable_field():
    legacy = {"translation_mode": "best_of_three", "precision_concurrency": 1}
    pipeline = PipelineConfig.model_validate(legacy)
    assert "precision_concurrency" not in pipeline.model_dump()
    assert "precision_concurrency" not in PipelineConfig.model_json_schema()["properties"]
    assert legacy["precision_concurrency"] == 1


def test_precision_defaults_and_constraints():
    assert PipelineConfig().translation_mode == "standard"
    assert "precision_concurrency" not in PipelineConfig().model_dump()
    with pytest.raises(ValidationError, match="requires pipeline.polish=true"):
        PipelineConfig(translation_mode="best_of_three", polish=False)


def test_precision_reuses_standard_translation_and_polish_routes_without_output_hints():
    standard = Config()
    precision = Config(pipeline=PipelineConfig(translation_mode="best_of_three", review=False))
    assert not REMOVED_OPERATIONS.intersection(OPERATIONS)
    assert not REMOVED_OPERATIONS.intersection(workflow_operations("translate", {}))
    body_operations = {"translation.body", "polish.body"}
    assert body_operations.issubset(configured_operations(standard, "translate"))
    assert body_operations.issubset(configured_operations(precision, "translate"))
    for workflow in ("prepare", "review", "srt"):
        assert not body_operations.intersection(configured_operations(precision, workflow))
    routes = resolve_routes(precision.llm)
    for operation in body_operations:
        assert OPERATIONS[operation].tier == "strong"
        assert OPERATIONS[operation].protocol_version == 2
        assert OPERATIONS[operation].output_tokens is None
        assert routes[operation].tier == "strong"


@pytest.mark.parametrize("operation", sorted(REMOVED_OPERATIONS))
def test_obsolete_precision_routes_fail_explicitly_without_rewriting_settings(operation):
    document = {"llm": {"preset": "fake", "routes": {operation: {"tier": "strong"}}}}
    with pytest.raises(ValidationError, match=f"Unknown model operation: {operation}"):
        Config.from_dict(document)
    assert document == {"llm": {"preset": "fake", "routes": {operation: {"tier": "strong"}}}}


def test_precision_freezes_every_consumed_prompt_and_configured_guidance():
    standard = Config()
    precision = Config(pipeline=PipelineConfig(translation_mode="best_of_three"))
    standard_plan = standard.language_policy("translation")
    plan = precision.language_policy("translation")
    expected = {
        "precision_generation_system",
        "precision_packet",
        "precision_translate",
        "precision_synthesize",
    }
    assert expected == {name for name, _ in plan.templates if name.startswith("precision_")}
    assert "translator_system" not in dict(plan.templates)
    assert "polisher_system" not in dict(plan.templates)
    assert not expected.intersection(dict(standard_plan.templates))
    assert plan.fingerprint != standard_plan.fingerprint
    assert not expected.intersection(
        dict(precision.language_policy("translation", path="srt").templates)
    )
    text = render(
        "precision_generation_system",
        src=precision.source_lang,
        tgt=precision.target_lang,
        plan=plan,
    )
    assert dict(plan.prompt_values)["configured_lang_guidance"] in text


def test_precision_cache_binds_consumed_templates_and_effective_language_guidance_only():
    plan = Config(pipeline=PipelineConfig(translation_mode="best_of_three")).language_policy(
        "translation"
    )
    fingerprint = plan.task_fingerprint("precision")
    unrelated = replace(
        plan,
        templates=tuple(
            (name, text + "\nUnrelated title rule." if name == "title_translator_user" else text)
            for name, text in plan.templates
        ),
    )
    assert unrelated.task_fingerprint("precision") == fingerprint
    effective = replace(
        plan,
        prompt_values=tuple(
            (
                name,
                value + "\nChanged address rule." if name == "configured_lang_guidance" else value,
            )
            for name, value in plan.prompt_values
        ),
    )
    assert effective.task_fingerprint("precision") != fingerprint


def test_cli_precision_conflict_is_concise_and_precedes_credentials():
    config = Config()
    with (
        patch("wenyi_cli.commands.context.CommandContext.load_config", return_value=config),
        patch("wenyi_cli.commands.context.CommandContext.validate_api_configuration") as validate,
    ):
        result = CliRunner().invoke(
            app, ["translate", "unused.txt", "--translation-mode", "best_of_three", "--no-polish"]
        )
    assert result.exit_code == 1
    assert "requires pipeline.polish=true" in result.output
    assert "Traceback" not in result.output
    validate.assert_not_called()


def test_srt_rejects_explicit_book_mode():
    result = CliRunner().invoke(
        app, ["translate", "unused.srt", "--translation-mode", "best_of_three"]
    )
    assert result.exit_code == 1
    assert "SRT translation does not support: --translation-mode" in result.output


def test_offline_model_preview_shows_shared_routes_without_removed_precision_operations():
    with patch("wenyi_cli.commands.context.CommandContext.load_config", return_value=Config()):
        result = CliRunner().invoke(app, ["models", "list", "--json"])
    assert result.exit_code == 0, result.output
    for operation in REMOVED_OPERATIONS:
        assert operation not in result.output
    for operation in ("translation.body", "polish.body"):
        assert operation in result.output


def test_cli_combines_mode_and_polish_overrides_before_validation():
    config = Config(pipeline=PipelineConfig(polish=False))
    with (
        patch("wenyi_cli.commands.context.CommandContext.load_config", return_value=config),
        patch("wenyi_cli.commands.context.CommandContext.validate_api_configuration") as validate,
        patch(
            "wenyi_cli.commands.workflows.require_input_file", side_effect=ValueError("stop here")
        ),
    ):
        result = CliRunner().invoke(
            app, ["translate", "unused.txt", "--translation-mode", "best_of_three", "--polish"]
        )
    assert result.exit_code == 1
    assert "stop here" in result.output
    assert config.pipeline.translation_mode == "best_of_three"
    assert config.pipeline.polish is True
    validate.assert_called_once()
