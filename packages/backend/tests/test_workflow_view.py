from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, cast

import pytest
from wenyi_backend.context import current_context
from wenyi_backend.emitters import RedisEmitter
from wenyi_backend.routers import configuration
from wenyi_core.events import TranslationEvent


@pytest.mark.parametrize("autofix, enabled", [(False, False), (True, True), (None, True)])
def test_workflow_uses_current_job_snapshot(monkeypatch, backend_context, autofix, enabled):
    monkeypatch.setattr(configuration, "require_project", lambda pid: {"id": pid, "fmt": "epub"})
    monkeypatch.setattr(
        current_context().repository,
        "latest_workflow_job",
        lambda pid: {
            "kind": "review",
            "status": "paused",
            "run_id": "run-a",
            "params": {
                "autofix": autofix,
                "config_snapshot": {"pipeline": {"review_autofix": True}},
            },
        },
    )

    def offline(pid):
        raise OSError("offline")

    monkeypatch.setattr(backend_context.telemetry, "progress_snapshot", offline)
    result = configuration.workflow("p")
    assert result["source"] == "snapshot"
    assert result["status"] == "paused"
    assert [(s["id"], s["enabled"]) for s in result["stages"]] == [
        ("review", True),
        ("review_autofix", enabled),
    ]
    assert result["progress"] is None


def test_progress_cache_carries_run_identity_and_cumulative_elapsed_time(monkeypatch):
    import json
    from datetime import datetime
    from types import SimpleNamespace

    from wenyi_backend import emitters

    ticks = iter([100.0, 104.5, 109.0])
    monkeypatch.setattr(emitters, "time", SimpleNamespace(monotonic=lambda: next(ticks)))

    class Redis:
        def set(self, key, value, ex):
            self.cached = (key, json.loads(value), ex)

        def publish(self, channel, value):
            self.published = json.loads(value)

    redis = Redis()
    emitter = RedisEmitter(cast(Any, redis), "p", "run-a")
    emitter.emit(TranslationEvent(kind="progress", label="batch", done=2, total=4))
    assert redis.cached[0] == "project:p:progress"
    assert redis.cached[1]["run_id"] == "run-a"
    assert redis.published["done"] == 2
    assert redis.published == redis.cached[1]
    assert redis.published["elapsed_seconds"] == 4.5
    assert datetime.fromisoformat(redis.published["updated_at"]).tzinfo is not None
    emitter.emit(TranslationEvent(kind="progress", label="next stage", done=0, total=2))
    assert redis.published["elapsed_seconds"] == 9.0


@pytest.mark.parametrize(
    "cached",
    [
        {"project_id": "p", "run_id": "old", "label": "Completed"},
        {"project_id": "other", "run_id": "new", "label": "Other book"},
    ],
)
def test_unrelated_progress_is_not_displayed(monkeypatch, backend_context, cached):
    monkeypatch.setattr(configuration, "require_project", lambda pid: {"id": pid, "fmt": "epub"})
    monkeypatch.setattr(
        current_context().repository,
        "latest_workflow_job",
        lambda pid: {
            "kind": "translation",
            "status": "queued",
            "run_id": "new",
            "params": {
                "config_snapshot": {"pipeline": {"review": False}},
            },
        },
    )

    monkeypatch.setattr(backend_context.telemetry, "progress_snapshot", lambda pid: cached)
    result = configuration.workflow("p")
    assert result["progress"] is None
    assert result["status"] == "queued"


@pytest.mark.parametrize(
    "status,run,seconds,quiet",
    [
        ("running", "current", 130, True),
        ("running", "current", 1, False),
        ("running", "older", 1, True),
        ("running", None, 0, True),
        ("queued", None, 0, False),
        ("paused", "current", 130, False),
    ],
)
def test_progress_silence_is_scoped_to_the_running_writer(
    monkeypatch, backend_context, status, run, seconds, quiet
):
    now = datetime.now(timezone.utc)
    job = {
        "kind": "translation",
        "status": status,
        "run_id": "current",
        "updated_at": (now - timedelta(minutes=3)).isoformat(),
        "params": {"config_snapshot": {"pipeline": {}}},
    }
    progress = (
        {
            "project_id": "p",
            "run_id": run,
            "updated_at": (now - timedelta(seconds=seconds)).isoformat(),
        }
        if run
        else None
    )
    monkeypatch.setattr(configuration, "require_project", lambda pid: {"id": pid})
    backend_context.repository.latest_workflow_job.return_value = job
    monkeypatch.setattr(backend_context.telemetry, "progress_snapshot", lambda pid: progress)
    assert configuration.workflow("p")["progress_quiet"] is quiet
