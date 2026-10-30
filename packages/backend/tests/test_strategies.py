"""Workflow configuration accepts only current dev capabilities."""

import pytest
from wenyi_backend.schemas import ExportRequest, ProjectCreate, ReviewRunRequest, StartTranslation
from wenyi_backend.strategies import PRESET_TEMPLATES, strategy_to_config
from wenyi_core.config import Config


def base():
    return Config.from_dict({"llm": {"preset": "fake"}})


def test_defaults_match_dev_and_preserve_language_direction():
    assert {row["name"] for row in PRESET_TEMPLATES} == {"标准翻译"}
    cfg = strategy_to_config({"template": "标准翻译"}, base(), source_lang="zh", target_lang="en")
    assert cfg.source_lang == "zh" and cfg.target_lang == "en"
    assert all(
        getattr(cfg.pipeline, key)
        for key in ("book_understanding", "polish", "review", "review_autofix")
    )
    with pytest.raises(ValueError, match="template"):
        strategy_to_config({"template": "快速出稿"}, base())
    with pytest.raises(ValueError, match="template"):
        strategy_to_config({"template": "快速出稿", "steps": {"polish": False}}, base())
    custom = strategy_to_config({"steps": {"review": False, "polish": True}}, base())
    assert custom.pipeline.polish and not custom.pipeline.review_autofix


def test_explicit_project_mode_does_not_inherit_precision_defaults():
    precision = base()
    precision.pipeline.translation_mode = "best_of_three"
    standard = strategy_to_config({}, precision, translation_mode="standard")
    assert standard.pipeline.translation_mode == "standard"
    assert precision.pipeline.translation_mode == "best_of_three"


def test_explicit_precision_choice_enables_polish_for_the_project():
    defaults = base()
    defaults.pipeline.polish = False
    precision = strategy_to_config(
        {"steps": {"polish": False}},
        defaults,
        translation_mode="best_of_three",
    )
    assert precision.pipeline.translation_mode == "best_of_three"
    assert precision.pipeline.polish is True
    assert "precision_concurrency" not in precision.pipeline.model_dump()
    assert defaults.pipeline.polish is False


def test_precision_cannot_disable_required_polishing():
    precision = base()
    precision.pipeline.translation_mode = "best_of_three"
    with pytest.raises(ValueError, match="polish"):
        strategy_to_config({"steps": {"polish": False}}, precision)


@pytest.mark.parametrize("step", ["backtranslate", "consistency_qa", "chapter_review", "autofix"])
def test_retired_steps_fail_instead_of_silently_ignoring(step):
    with pytest.raises(ValueError):
        strategy_to_config({"steps": {step: True}}, base())


@pytest.mark.parametrize(
    "model,body",
    [
        (StartTranslation, {"do_qa": True}),
        (ReviewRunRequest, {"force": True}),
        (ExportRequest, {"format": "invalid"}),
        (ProjectCreate, {"name": "x", "target_lang": "auto"}),
    ],
)
def test_invalid_or_removed_request_fields_are_rejected(model, body):
    with pytest.raises(ValueError):
        model.model_validate(body)
