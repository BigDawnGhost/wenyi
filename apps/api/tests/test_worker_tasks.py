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
