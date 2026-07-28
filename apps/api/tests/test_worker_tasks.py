"""Worker 任务状态流转测试（不依赖 DB / Redis）。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from wenyi_api.workers import tasks


def test_run_translation_marks_startup_failure_as_error(monkeypatch):
    statuses: list[tuple[str, str]] = []

    monkeypatch.setattr(
        tasks.dal,
        "set_project_status",
        lambda pid, status: statuses.append((pid, status)),
    )

    def fail_before_pipeline(project_id: str, *, do_qa: bool | None = None) -> None:
        raise SyntaxError("failed to import orchestrator")

    monkeypatch.setattr(tasks, "_translate_sync", fail_before_pipeline)

    with pytest.raises(SyntaxError, match="failed to import orchestrator"):
        asyncio.run(tasks.run_translation({}, project_id="project-1"))

    assert statuses == [
        ("project-1", "translating"),
        ("project-1", "error"),
    ]


def test_run_prepare_marks_startup_failure_as_error(monkeypatch):
    statuses: list[tuple[str, str]] = []

    monkeypatch.setattr(
        tasks.dal,
        "set_project_status",
        lambda pid, status: statuses.append((pid, status)),
    )

    def fail_before_pipeline(project_id: str) -> None:
        raise RuntimeError("config missing")

    monkeypatch.setattr(tasks, "_prepare_sync", fail_before_pipeline)

    with pytest.raises(RuntimeError, match="config missing"):
        asyncio.run(tasks.run_prepare({}, project_id="project-1"))

    assert statuses == [
        ("project-1", "preparing"),
        ("project-1", "error"),
    ]


def test_run_review_marks_startup_failure_as_error(monkeypatch):
    statuses: list[tuple[str, str]] = []

    monkeypatch.setattr(
        tasks.dal,
        "set_project_status",
        lambda pid, status: statuses.append((pid, status)),
    )

    def fail_before_pipeline(
        project_id: str, *, force: bool = False, autofix: bool = True
    ) -> None:
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr(tasks, "_review_sync", fail_before_pipeline)

    with pytest.raises(RuntimeError, match="storage unavailable"):
        asyncio.run(tasks.run_review({}, project_id="project-1"))

    assert statuses == [
        ("project-1", "reviewing"),
        ("project-1", "error"),
    ]


def test_run_qa_marks_startup_failure_as_error(monkeypatch):
    statuses: list[tuple[str, str]] = []
    errors: list[tuple[str, str, str]] = []

    monkeypatch.setattr(
        tasks.dal,
        "set_project_status",
        lambda pid, status: statuses.append((pid, status)),
    )

    def fail_before_check(
        project_id: str, *, completion_status: str = "done"
    ) -> None:
        raise RuntimeError("LLM unavailable")

    monkeypatch.setattr(tasks, "_qa_sync", fail_before_check)
    monkeypatch.setattr(
        tasks,
        "_record_qa_error",
        lambda pid, error, *, completion_status: errors.append(
            (pid, str(error), completion_status)
        ),
    )

    with pytest.raises(RuntimeError, match="LLM unavailable"):
        asyncio.run(tasks.run_qa({}, project_id="project-1"))

    assert statuses == [
        ("project-1", "qa"),
        ("project-1", "error"),
    ]
    assert errors == [("project-1", "LLM unavailable", "done")]


def test_record_qa_error_replaces_running_state(monkeypatch):
    reports: list[dict] = [
        {
            "consistency_qa": {
                "status": "running",
                "completion_status": "reviewed",
            }
        }
    ]
    events: list[tuple[str, dict]] = []
    storage = SimpleNamespace(
        load_report=lambda: dict(reports[-1]),
        save_report=lambda report: reports.append(dict(report)),
        log_event=lambda event, **data: events.append((event, data)),
    )
    monkeypatch.setattr(tasks, "init_pool", lambda dsn: "pool")
    monkeypatch.setattr(tasks, "_pipeline_storage", lambda pid, pool: storage)

    tasks._record_qa_error(
        "project-1",
        RuntimeError("config missing"),
        completion_status="reviewed",
    )

    assert reports[-1]["consistency_qa"] == {
        "status": "error",
        "completion_status": "reviewed",
        "error": "config missing",
    }
    assert events == [
        ("consistency_qa_error", {"error": "config missing"}),
    ]


def test_qa_sync_checks_and_persists_report(monkeypatch):
    import redis as redis_lib
    import wenyi_core.agents.consistency as consistency_mod
    import wenyi_core.assemble.report as report_mod
    import wenyi_core.llm.factory as factory_mod

    saved: list[dict] = []
    events: list[tuple[str, dict]] = []
    progress: list[tuple[int, int, str]] = []
    statuses: list[tuple[str, str]] = []
    storage = SimpleNamespace(
        load_report=lambda: dict(saved[-1]) if saved else {},
        save_report=lambda report: saved.append(dict(report)),
        log_event=lambda event, **data: events.append((event, data)),
    )
    issues = [{"type": "pronoun", "detail": "代词不一致", "where": "第一、二章"}]

    class FakeChecker:
        def __init__(self, client, config):
            assert client == "client"
            assert config == "config"

        def check_and_record(self, current_storage):
            assert current_storage is storage
            events.append(
                (
                    "consistency_qa_finished",
                    {"issue_count": 1, "issues": issues},
                )
            )
            return issues

    monkeypatch.setattr(tasks, "init_pool", lambda dsn: "pool")
    monkeypatch.setattr(tasks, "_build_config_for", lambda pid: "config")
    monkeypatch.setattr(tasks, "_pipeline_storage", lambda pid, pool: storage)
    monkeypatch.setattr(
        tasks,
        "redis_progress_fn",
        lambda redis, pid, *, kind: (
            lambda done, total, label: progress.append((done, total, label))
        ),
    )
    monkeypatch.setattr(
        tasks.dal,
        "set_project_status",
        lambda pid, status: statuses.append((pid, status)),
    )
    monkeypatch.setattr(redis_lib, "from_url", lambda url: "redis")
    monkeypatch.setattr(factory_mod, "build_client", lambda cfg: "client")
    monkeypatch.setattr(consistency_mod, "ConsistencyChecker", FakeChecker)
    monkeypatch.setattr(
        report_mod,
        "build_report",
        lambda current_storage, *, consistency_issues: {
            "summary": {"chapters_done": 2},
            "consistency_issues": consistency_issues,
        },
    )

    tasks._qa_sync("project-1", completion_status="reviewed")

    assert saved == [
        {
            "summary": {"chapters_done": 2},
            "consistency_issues": issues,
        },
        {
            "summary": {"chapters_done": 2},
            "consistency_issues": issues,
            "consistency_qa": {
                "status": "completed",
                "completion_status": "reviewed",
            },
        }
    ]
    assert events == [
        (
            "consistency_qa_finished",
            {"issue_count": 1, "issues": issues},
        )
    ]
    assert progress == [
        (0, 1, "一致性检查中…"),
        (1, 1, "一致性检查完成：发现 1 项问题"),
    ]
    assert statuses == [("project-1", "reviewed")]


def test_resolve_source_uses_recorded_path_without_parsed_title(
    monkeypatch, tmp_path
):
    source = tmp_path / "project-1" / "source.html"
    source.parent.mkdir()
    source.write_text("<h1>Chapter</h1>", encoding="utf-8")
    monkeypatch.setattr(
        tasks.dal,
        "get_project",
        lambda pid: {
            "id": pid,
            "title": None,
            "source_path": "project-1/source.html",
        },
    )
    monkeypatch.setattr(
        tasks,
        "settings",
        SimpleNamespace(data_dir=str(tmp_path)),
    )

    assert tasks._resolve_source("project-1") == str(source)


def test_prepare_sync_uses_prepare_for_translation(monkeypatch):
    """译前准备应走 prepare_for_translation（含全书概览），而非仅 prepare。"""
    import redis as redis_lib
    import wenyi_core.llm.factory as factory_mod
    import wenyi_core.pipeline.orchestrator as orch_mod

    calls: list[str] = []

    class FakeOrch:
        def __init__(self, *args, **kwargs):
            pass

        def prepare(self, *args, **kwargs):
            calls.append("prepare")

        def prepare_for_translation(self, *args, **kwargs):
            calls.append("prepare_for_translation")

    monkeypatch.setattr(tasks, "_build_config_for", lambda pid: object())
    monkeypatch.setattr(tasks, "_resolve_source", lambda pid: "/tmp/x.epub")
    monkeypatch.setattr(tasks, "init_pool", lambda dsn: object())
    monkeypatch.setattr(tasks, "_pipeline_storage", lambda pid, pool: object())
    monkeypatch.setattr(tasks.dal, "set_project_status", lambda pid, status: None)
    monkeypatch.setattr(
        tasks, "_progress_with_pause", lambda redis, pid: (lambda *a, **k: None)
    )
    monkeypatch.setattr(factory_mod, "build_client", lambda cfg: object())
    monkeypatch.setattr(orch_mod, "Orchestrator", FakeOrch)
    monkeypatch.setattr(redis_lib, "from_url", lambda url: object())

    tasks._prepare_sync("project-1")
    assert calls == ["prepare_for_translation"]
