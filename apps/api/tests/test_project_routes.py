"""项目启动路由的契约测试（不依赖 DB / Redis）。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from wenyi_api.routers import projects
from wenyi_api.schemas import StartTranslation


def test_start_translation_persists_selected_strategy(monkeypatch):
    saved: list[tuple[str, dict]] = []
    enqueued: list[tuple[str, dict]] = []

    monkeypatch.setattr(projects.dal, "get_project", lambda pid: {"id": pid})
    monkeypatch.setattr(
        projects.dal,
        "set_project_strategy",
        lambda pid, strategy: saved.append((pid, strategy)),
    )
    monkeypatch.setattr(projects, "set_project_status", lambda pid, status: None)

    async def fake_enqueue(name: str, **kwargs):
        enqueued.append((name, kwargs))
        return SimpleNamespace(job_id="job-1")

    monkeypatch.setattr(projects, "enqueue", fake_enqueue)

    result = asyncio.run(
        projects.start_translation(
            "project-1",
            StartTranslation(
                strategy={"template": "精翻"},
                do_qa=True,
            ),
        )
    )

    assert saved == [("project-1", {"template": "精翻"})]
    assert enqueued == [
        ("run_translation", {"project_id": "project-1", "do_qa": True})
    ]
    assert result["job_id"] == "job-1"


def test_start_translation_without_body_keeps_existing_strategy(monkeypatch):
    monkeypatch.setattr(projects.dal, "get_project", lambda pid: {"id": pid})
    monkeypatch.setattr(
        projects.dal,
        "set_project_strategy",
        lambda pid, strategy: (_ for _ in ()).throw(AssertionError("unexpected update")),
    )
    monkeypatch.setattr(projects, "set_project_status", lambda pid, status: None)

    async def fake_enqueue(name: str, **kwargs):
        return SimpleNamespace(job_id="job-2")

    monkeypatch.setattr(projects, "enqueue", fake_enqueue)

    result = asyncio.run(projects.start_translation("project-1"))

    assert result["job_id"] == "job-2"
