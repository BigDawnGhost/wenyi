"""Project precision policy is independent of the platform settings adapter."""

import pytest
from wenyi_backend.config_documents import project_document
from wenyi_backend.global_settings import GlobalSettings, validate_settings
from wenyi_backend.project_service import effective_config
from wenyi_backend.routers import settings
from wenyi_core.config import Config


def test_global_configuration_does_not_expose_project_precision_choices():
    config = Config.from_dict({"llm": {"preset": "fake"}})
    response = settings.response(GlobalSettings(config))
    assert "translation_mode" not in response["effective"]["pipeline"]
    assert "precision_concurrency" not in response["effective"]["pipeline"]
    assert "translation_mode" not in response["yaml"]
    assert "precision_concurrency" not in response["yaml"]


@pytest.mark.parametrize(
    "field,value", [("translation_mode", "best_of_three"), ("precision_concurrency", 1)]
)
def test_global_yaml_rejects_project_precision_choices(field, value):
    with pytest.raises(ValueError, match="project"):
        validate_settings(f"llm: {{preset: fake}}\npipeline: {{{field}: {value}}}", "标准翻译")


def test_global_yaml_rejects_retired_template():
    with pytest.raises(ValueError, match="template"):
        validate_settings("llm: {preset: fake}", "快速出稿")


def test_saved_project_precision_survives_standard_defaults(tmp_path, monkeypatch):
    from wenyi_backend import project_service

    monkeypatch.setattr(project_service.paths, "project_dir", lambda pid: str(tmp_path / pid))
    precision = Config.from_dict(
        {
            "llm": {"preset": "fake"},
            "pipeline": {"translation_mode": "best_of_three", "precision_concurrency": 1},
        }
    )
    project = {"id": "saved-precision", "config": project_document(precision)}
    defaults = Config.from_dict({"llm": {"preset": "fake"}})
    restored = effective_config(project, defaults=defaults)
    assert restored.pipeline.translation_mode == "best_of_three"
    assert "precision_concurrency" not in restored.pipeline.model_dump()
    assert project["config"]["pipeline"]["translation_mode"] == "best_of_three"
    assert defaults.pipeline.translation_mode == "standard"
