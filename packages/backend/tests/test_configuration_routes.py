"""Configuration endpoints expose routing tools and workflow statistics."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from wenyi_backend.application import create_app
from wenyi_backend.context import current_context
from wenyi_backend.project_service import effective_config
from wenyi_backend.routers import configuration
from wenyi_core.config import Config
from wenyi_core.llm.usage import UsageSample, UsageTracker, empty_usage


def test_model_comparison_endpoints_are_not_available(backend_context):
    app = create_app(backend_context)
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
        "config": {"pipeline": {"translation_mode": "three_draft_synthesis", "polish": True}},
    }
    valid = effective_config(
        project,
        defaults=defaults,
        state_dir="unused",
        document={"pipeline": {"translation_mode": "three_draft_synthesis", "polish": True}},
    )
    assert valid.pipeline.translation_mode == "three_draft_synthesis"
    for pipeline in (
        {"translation_mode": "three_draft_synthesis", "polish": False},
        {"translation_mode": "invalid"},
        {"translation_mode": "standard"},
    ):
        with pytest.raises(ValueError):
            effective_config(
                project, defaults=defaults, state_dir="unused", document={"pipeline": pipeline}
            )

    for document in ({}, {"pipeline": {"review": False}}):
        restored = effective_config(
            project, defaults=defaults, state_dir="unused", document=document
        )
        assert restored.pipeline.translation_mode == "three_draft_synthesis"
        assert restored.pipeline.polish is True


def test_batch_glossary_persists_in_global_and_project_settings():
    from wenyi_backend.config_documents import global_document, project_document

    defaults = Config.from_dict(
        {"llm": {"preset": "fake"}, "pipeline": {"batch_glossary": "relevant"}}
    )
    assert global_document(defaults)["pipeline"]["batch_glossary"] == "relevant"

    project = {
        "id": "batch-glossary-test",
        "source_lang": "zh",
        "target_lang": "en",
        "config": {},
    }
    inherited = effective_config(project, defaults=defaults, state_dir="unused")
    assert inherited.pipeline.batch_glossary == "relevant"
    assert project_document(inherited)["pipeline"]["batch_glossary"] == "relevant"

    overridden = effective_config(
        project,
        defaults=defaults,
        state_dir="unused",
        document={"pipeline": {"batch_glossary": "all"}},
    )
    assert overridden.pipeline.batch_glossary == "all"
    assert project_document(overridden)["pipeline"]["batch_glossary"] == "all"

    with pytest.raises(ValueError):
        effective_config(
            project,
            defaults=defaults,
            state_dir="unused",
            document={"pipeline": {"batch_glossary": "invalid"}},
        )


def test_nextgen_pipeline_settings_persist_in_global_and_project_settings(monkeypatch):
    from wenyi_backend.config_documents import global_document, project_document

    defaults = Config.from_dict(
        {
            "llm": {"preset": "fake"},
            "pipeline": {
                "glossary_mode": "compiled",
                "polish_mode": "sparse",
                "chapter_concurrency": 4,
                "consistency_sweep": True,
                "deterministic_hygiene": False,
            },
        }
    )
    global_pipeline = global_document(defaults)["pipeline"]
    assert global_pipeline["glossary_mode"] == "compiled"
    assert global_pipeline["polish_mode"] == "sparse"
    assert global_pipeline["chapter_concurrency"] == 4
    assert global_pipeline["consistency_sweep"] is True
    assert global_pipeline["deterministic_hygiene"] is False

    project = {
        "id": "nextgen-settings-test",
        "source_lang": "zh",
        "target_lang": "en",
        "config": {},
    }
    inherited = effective_config(project, defaults=defaults, state_dir="unused")
    assert inherited.pipeline.glossary_mode == "compiled"
    assert inherited.pipeline.polish_mode == "sparse"
    assert inherited.pipeline.chapter_concurrency == 4
    assert inherited.pipeline.consistency_sweep is True
    assert inherited.pipeline.deterministic_hygiene is False
    assert project_document(inherited)["pipeline"]["glossary_mode"] == "compiled"
    assert project_document(inherited)["pipeline"]["consistency_sweep"] is True

    overridden = effective_config(
        project,
        defaults=defaults,
        state_dir="unused",
        document={
            "pipeline": {
                "glossary_mode": "incremental",
                "polish_mode": "full",
                "chapter_concurrency": 1,
                "consistency_sweep": False,
                "deterministic_hygiene": True,
            }
        },
    )
    assert overridden.pipeline.glossary_mode == "incremental"
    assert overridden.pipeline.polish_mode == "full"
    assert overridden.pipeline.chapter_concurrency == 1
    assert overridden.pipeline.consistency_sweep is False
    assert overridden.pipeline.deterministic_hygiene is True

    for invalid_pipeline in (
        {"glossary_mode": "invalid"},
        {"polish_mode": "invalid"},
        {"chapter_concurrency": 0},
    ):
        with pytest.raises(ValueError):
            effective_config(
                project,
                defaults=defaults,
                state_dir="unused",
                document={"pipeline": invalid_pipeline},
            )

    monkeypatch.setattr(configuration, "require_project", lambda _: {"id": "nextgen-wf"})
    monkeypatch.setattr(
        current_context().repository,
        "latest_workflow_job",
        lambda _: {
            "kind": "translation",
            "status": "queued",
            "params": {
                "config_snapshot": {
                    "pipeline": {
                        "book_understanding": True,
                        "polish": True,
                        "consistency_sweep": True,
                        "review": False,
                        "review_autofix": False,
                    }
                }
            },
        },
    )
    wf = configuration.workflow("nextgen-wf")
    stage_ids = [s["id"] for s in wf["stages"]]
    assert "consistency_sweep" in stage_ids
    sweep_stage = next(s for s in wf["stages"] if s["id"] == "consistency_sweep")
    assert sweep_stage["enabled"] is True

    monkeypatch.setattr(
        current_context().repository,
        "latest_workflow_job",
        lambda _: {
            "kind": "chapter_translation",
            "status": "queued",
            "params": {
                "config_snapshot": {
                    "pipeline": {
                        "book_understanding": True,
                        "polish": True,
                        "consistency_sweep": True,
                        "review": False,
                        "review_autofix": False,
                    }
                }
            },
        },
    )
    chapter_wf = configuration.workflow("nextgen-wf")
    assert "consistency_sweep" not in [s["id"] for s in chapter_wf["stages"]]


@pytest.mark.parametrize(
    "strategy",
    [
        {"template": "快速出稿"},
        {"template": "标准翻译", "steps": {"report": True}},
    ],
)
def test_project_read_does_not_alias_removed_workflow_settings(strategy):
    from copy import deepcopy

    project = {"id": "legacy", "strategy": strategy}
    before = deepcopy(project)
    with pytest.raises(ValueError):
        effective_config(
            project, defaults=Config.from_dict({"llm": {"preset": "fake"}}), state_dir="unused"
        )
    assert project == before


def test_workflow_keeps_frozen_quick_snapshot_independent(monkeypatch):
    from copy import deepcopy

    snapshot = {
        "pipeline": dict.fromkeys(
            ("book_understanding", "polish", "review", "review_autofix"), False
        )
    }
    before = deepcopy(snapshot)
    monkeypatch.setattr(configuration, "require_project", lambda _: {"id": "legacy"})
    monkeypatch.setattr(
        current_context().repository,
        "latest_workflow_job",
        lambda _: {
            "kind": "translation",
            "status": "queued",
            "params": {"config_snapshot": snapshot},
        },
    )
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

    closed = []
    storage = SimpleNamespace(
        load_usage=lambda: usage, read_artifact=read_artifact, close=lambda: closed.append(True)
    )
    monkeypatch.setattr(configuration, "require_project", lambda pid: {"id": pid})
    monkeypatch.setattr(configuration, "storage_for", lambda pid: storage)
    expected = {
        "usage": usage or empty_usage(),
        "timing": timing if initialized else {"runs": [], "total_seconds": 0},
    }
    assert configuration.project_stats("test") == expected
    assert configuration.project_stats("test") == expected
    assert closed == [True, True]
