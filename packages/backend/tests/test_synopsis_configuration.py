"""Synopsis budgets survive shared YAML validation and execution snapshots."""

from wenyi_backend.config_documents import config_from_document, runtime_document
from wenyi_backend.global_settings import validate_settings
from wenyi_backend.project_service import effective_config
from wenyi_core.config import Config


def test_global_yaml_accepts_synopsis_token_budgets():
    config = validate_settings(
        "llm: {preset: fake}\n"
        "pipeline:\n"
        "  synopsis_source_tokens: 16000\n"
        "  synopsis_reduce_tokens: 24000\n",
        "标准翻译",
    )
    assert config.pipeline.synopsis_source_tokens == 16000
    assert config.pipeline.synopsis_reduce_tokens == 24000


def test_project_synopsis_budgets_survive_execution_snapshot(tmp_path):
    budgets = {"synopsis_source_tokens": 16000, "synopsis_reduce_tokens": 24000}
    project = {"id": "synopsis-budgets", "source_lang": "en", "target_lang": "zh"}
    config = effective_config(
        project,
        defaults=Config.from_dict({"llm": {"preset": "fake"}}),
        state_dir=str(tmp_path / "state"),
        document={"pipeline": budgets},
    )
    snapshot = runtime_document(config)
    restored = config_from_document(snapshot)
    for key, value in budgets.items():
        assert snapshot["pipeline"][key] == value
        assert getattr(restored.pipeline, key) == value
