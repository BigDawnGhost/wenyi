"""Configuration endpoints expose routing tools and workflow statistics."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from wenyi_api import main
from wenyi_api.project_service import effective_config
from wenyi_api.routers import configuration
from wenyi_core.config import Config
from wenyi_core.llm.usage import UsageSample, UsageTracker, empty_usage


def test_model_comparison_endpoints_are_not_available(monkeypatch):
    monkeypatch.setattr(main, "settings", replace(main.settings, api_token=None))
    app = main.create_app()
    client = TestClient(app)
    try:
        assert client.post("/projects/test/models/compare", json={}).status_code == 404
        assert client.get("/projects/test/models/comparisons/test").status_code == 404
    finally:
        client.close()
    schema = app.openapi()
    assert "ModelCompareRequest" not in schema["components"]["schemas"]
    assert "ModelMessage" not in schema["components"]["schemas"]
    assert "/projects/{pid}/models/compare" not in schema["paths"]
    assert "/projects/{pid}/models/comparisons/{job_id}" not in schema["paths"]
    assert "/projects/{pid}/models" in schema["paths"]
    assert "/projects/{pid}/models/check" in schema["paths"]


def test_precision_project_yaml_validates_mode_and_required_polish():
    defaults = Config.from_dict({"llm": {"preset": "fake"}})
    project = {
        "id": "precision-test",
        "source_lang": "en",
        "target_lang": "zh",
        "config": {"pipeline": {"translation_mode": "best_of_three", "polish": True}},
    }
    valid = effective_config(
        project,
        defaults=defaults,
        document={"pipeline": {"translation_mode": "best_of_three", "polish": True}},
    )
    assert valid.pipeline.translation_mode == "best_of_three"
    for pipeline in (
        {"translation_mode": "best_of_three", "polish": False},
        {"translation_mode": "invalid"},
        {"translation_mode": "standard"},
    ):
        with pytest.raises(ValueError):
            effective_config(project, defaults=defaults, document={"pipeline": pipeline})

    for document in ({}, {"pipeline": {"review": False}}):
        restored = effective_config(project, defaults=defaults, document=document)
        assert restored.pipeline.translation_mode == "best_of_three"
        assert restored.pipeline.polish is True


def test_legacy_quick_project_read_preserves_saved_configuration():
    from copy import deepcopy

    project = {"id": "legacy", "strategy": {"template": "快速出稿"}}
    before = deepcopy(project)
    config = effective_config(project, defaults=Config.from_dict({"llm": {"preset": "fake"}}))
    assert not any(
        getattr(config.pipeline, key)
        for key in ("book_understanding", "polish", "review", "review_autofix")
    )
    assert project == before
    project["config"] = {"pipeline": {"polish": True}}
    assert effective_config(
        project, defaults=Config.from_dict({"llm": {"preset": "fake"}})
    ).pipeline.polish


def test_workflow_keeps_frozen_quick_snapshot_independent(monkeypatch):
    from copy import deepcopy

    from redis import Redis

    snapshot = {
        "pipeline": dict.fromkeys(
            ("book_understanding", "polish", "review", "review_autofix"), False
        )
    }
    before = deepcopy(snapshot)
    monkeypatch.setattr(configuration, "require_project", lambda _: {"id": "legacy"})
    monkeypatch.setattr(
        configuration.dal,
        "list_jobs",
        lambda _: [
            {"kind": "translation", "status": "queued", "params": {"config_snapshot": snapshot}}
        ],
    )
    monkeypatch.setattr(Redis, "get", lambda *_: None)
    result = configuration.workflow("legacy")
    assert result["source"] == "snapshot"
    assert all(
        not stage["enabled"] for stage in result["stages"] if stage["id"] in snapshot["pipeline"]
    )
    assert snapshot == before


@pytest.mark.parametrize("initialized", [False, True])
def test_project_stats_read_workflow_usage_and_timing(monkeypatch, initialized):
    tracker = UsageTracker()
    tracker.record(
        "fast", UsageSample(prompt_tokens=2, completion_tokens=3, total_tokens=5), "Translator"
    )
    usage = tracker.summary() if initialized else None
    timing = {"runs": [{"id": "translation", "elapsed_seconds": 8}], "total_seconds": 8}

    def read_artifact(key):
        assert key == "timing.json"
        return timing if initialized else None

    storage = SimpleNamespace(load_usage=lambda: usage, read_artifact=read_artifact)
    monkeypatch.setattr(configuration, "require_project", lambda pid: {"id": pid})
    monkeypatch.setattr(configuration, "storage_for", lambda pid: storage)
    expected = {
        "usage": usage or empty_usage(),
        "timing": timing if initialized else {"runs": [], "total_seconds": 0},
    }
    assert configuration.project_stats("test") == expected
    assert configuration.project_stats("test") == expected
