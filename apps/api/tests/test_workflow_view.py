from __future__ import annotations

from typing import Any, cast

from wenyi_api.emitters import RedisEmitter
from wenyi_api.routers import configuration
from wenyi_core.events import TranslationEvent


def test_workflow_uses_snapshot_and_excludes_exports(monkeypatch):
    monkeypatch.setattr(configuration, "require_project", lambda pid: {"id": pid, "fmt": "epub"})
    monkeypatch.setattr(
        configuration.dal,
        "list_jobs",
        lambda pid: [
            {"kind": "export"},
            {
                "kind": "review",
                "status": "paused",
                "run_id": "run-a",
                "params": {
                    "autofix": False,
                    "config_snapshot": {"pipeline": {"review_autofix": True}},
                },
            },
        ],
    )
    import redis

    class OfflineRedis:
        @staticmethod
        def from_url(*args, **kwargs):
            raise OSError("offline")

    monkeypatch.setattr(redis, "Redis", OfflineRedis)
    result = configuration.workflow("p")
    assert result["source"] == "snapshot"
    assert result["status"] == "paused"
    assert [(s["id"], s["enabled"]) for s in result["stages"]] == [
        ("review", True),
        ("review_autofix", False),
        ("report", True),
    ]
    assert result["progress"] is None


def test_subtitle_plan_does_not_show_book_steps(monkeypatch):
    monkeypatch.setattr(configuration, "require_project", lambda pid: {"id": pid, "fmt": "srt"})
    monkeypatch.setattr(configuration.dal, "list_jobs", lambda pid: [])
    monkeypatch.setattr(configuration, "effective_config", lambda project: None)
    monkeypatch.setattr(configuration, "config_document", lambda config: {"pipeline": {}})
    result = configuration.workflow("p")
    assert result["source"] == "config"
    assert result["status"] == "not_started"
    assert [s["id"] for s in result["stages"]] == ["srt", "assemble"]


def test_progress_cache_carries_run_identity():
    import json

    class Redis:
        def set(self, key, value, ex):
            self.cached = (key, json.loads(value), ex)

        def publish(self, channel, value):
            self.published = json.loads(value)

    redis = Redis()
    RedisEmitter(cast(Any, redis), "p", "run-a").emit(
        TranslationEvent(kind="progress", label="batch", done=2, total=4)
    )
    assert redis.cached[0] == "project:p:progress"
    assert redis.cached[1]["run_id"] == "run-a"
    assert redis.published["done"] == 2


def test_progress_from_previous_run_is_not_displayed(monkeypatch):
    import json

    import redis

    monkeypatch.setattr(configuration, "require_project", lambda pid: {"id": pid, "fmt": "epub"})
    monkeypatch.setattr(
        configuration.dal,
        "list_jobs",
        lambda pid: [
            {
                "kind": "translation",
                "status": "queued",
                "run_id": "new",
                "params": {
                    "config_snapshot": {"pipeline": {"review": False}},
                },
            },
        ],
    )

    class CachedRedis:
        @classmethod
        def from_url(cls, *args, **kwargs):
            return cls()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, key):
            return json.dumps({"run_id": "old", "label": "任务完成"})

    monkeypatch.setattr(redis, "Redis", CachedRedis)
    result = configuration.workflow("p")
    assert result["progress"] is None
    assert result["status"] == "queued"
