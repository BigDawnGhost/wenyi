"""Live views must not replay already persisted usage or count paused time."""

import json
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from wenyi_api.live_statistics import LiveStatistics, read_live_statistics
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.llm.usage import UsageSample, empty_usage
from wenyi_core.timing import RunTimer, observe_timers


class MemoryRedis:
    def __init__(self):
        self.values = {}
        self.messages = []

    def set(self, key, value, ex):
        self.values[key] = value

    def get(self, key):
        return self.values.get(key)

    def publish(self, channel, value):
        self.messages.append(json.loads(value))


class MemoryStore:
    def __init__(self):
        self.usage = empty_usage()
        self.timing = {"total_seconds": 20, "runs": [{"id": "old", "elapsed_seconds": 20}]}

    def load_usage(self):
        return deepcopy(self.usage)

    def read_artifact(self, key):
        assert key == "timing.json"
        return deepcopy(self.timing)

    def record_timing(self, record):
        runs = {row["id"]: row for row in self.timing["runs"]}
        runs[record["id"]] = record
        self.timing = {
            "runs": list(runs.values()),
            "total_seconds": sum(row["elapsed_seconds"] for row in runs.values()),
        }
        return deepcopy(self.timing)


def test_live_usage_is_visible_before_flush_and_not_added_twice_after_flush():
    store, redis, client = MemoryStore(), MemoryRedis(), FakeClient()
    client.usage.record("fast", UsageSample(2, 3, 5), "translation.body")
    store.usage = client.usage_summary()
    live = LiveStatistics(redis, "book", "run", store)
    live.bind_client(client)
    client.usage.record("fast", UsageSample(4, 6, 10), "translation.body")
    live.publish()
    assert json.loads(redis.get("project:book:stats"))["usage"]["totals"]["total_tokens"] == 15
    # A normal checkpoint writes the same calls; the live view must not add them again.
    store.usage = client.usage_summary()
    live.publish()
    assert json.loads(redis.get("project:book:stats"))["usage"]["totals"]["total_tokens"] == 15
    assert store.usage["totals"]["total_tokens"] == 15
    assert redis.messages[-1]["run_id"] == "run"


def test_live_time_uses_the_core_timer_identity_and_freezes_after_exit():
    store, redis = MemoryStore(), MemoryRedis()
    live = LiveStatistics(redis, "book", "run", store)
    now = 100.0
    with observe_timers(live.observe_timer):
        with RunTimer("workflow", clock=lambda: now) as timer:
            timer.store = store
            now += 7
            live.publish()
            snapshot = json.loads(redis.get("project:book:stats"))
            assert snapshot["timing"]["total_seconds"] == 27
            assert snapshot["timing"]["runs"][-1]["status"] == "running"
            assert len(store.timing["runs"]) == 1
        now += 500
        live.publish()
    assert json.loads(redis.get("project:book:stats"))["timing"]["total_seconds"] == 27
    assert len(store.timing["runs"]) == 2
    # Resuming starts a new timer and excludes the paused interval.
    with observe_timers(live.observe_timer):
        with RunTimer("workflow", clock=lambda: now) as resumed:
            resumed.store = store
            now += 3
            live.publish()
            assert json.loads(redis.get("project:book:stats"))["timing"]["total_seconds"] == 30


@pytest.mark.parametrize(
    "status, run_id, project_id, age",
    [
        ("paused", "run", "book", 0),
        ("done", "run", "book", 0),
        ("queued", "run", "book", 0),
        ("running", "old", "book", 0),
        ("running", "run", "other", 0),
        ("running", "run", "book", 60),
    ],
)
def test_live_reader_rejects_stopped_unrelated_and_stale_runs(status, run_id, project_id, age):
    redis = MemoryRedis()
    from datetime import timedelta

    redis.values["project:book:stats"] = json.dumps(
        {
            "project_id": project_id,
            "run_id": run_id,
            "updated_at": (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat(),
            "usage": empty_usage(),
            "timing": {"runs": [], "total_seconds": 5},
        }
    )
    job = {"run_id": "run", "status": status}
    assert read_live_statistics(redis, "book", job) is None


def test_live_reader_returns_current_snapshot_without_writing_ledgers():
    redis, store = MemoryRedis(), MemoryStore()
    live = LiveStatistics(redis, "book", "run", store)
    live.publish()
    result = read_live_statistics(redis, "book", {"run_id": "run", "status": "running"})
    assert result is not None
    assert result["live"]["run_id"] == "run"
    assert 0 < result["live"]["valid_for_seconds"] <= 10
    assert result["timing"]["total_seconds"] == 20


def test_redis_failure_does_not_interrupt_the_workflow():
    redis = SimpleNamespace(set=lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")))
    live = LiveStatistics(redis, "book", "run", MemoryStore())
    live.publish()


def test_observer_scope_does_not_escape_to_unrelated_invocations():
    seen = []
    with observe_timers(seen.append):
        with RunTimer("review") as observed:
            pass
    with RunTimer("translate"):
        pass
    assert seen == [observed]


@pytest.mark.parametrize("status", ["running", "paused", "done"])
def test_stats_endpoint_uses_current_job_and_falls_back_to_durable_totals(monkeypatch, status):
    from redis import Redis
    from wenyi_api.routers import configuration
    from wenyi_api.schemas import ProjectStats

    redis, store, client = MemoryRedis(), MemoryStore(), FakeClient()
    live = LiveStatistics(redis, "book", "run", store)
    live.bind_client(client)
    client.usage.record("fast", UsageSample(4, 6, 10), "translation.body")
    live.publish()
    monkeypatch.setattr(
        configuration, "require_project", lambda _: {"id": "book", "status": "translating"}
    )
    monkeypatch.setattr(
        configuration.dal,
        "list_jobs",
        lambda _: [
            {"kind": "export", "run_id": "export", "status": "running"},
            {"kind": "translation", "run_id": "run", "status": status},
        ],
    )
    monkeypatch.setattr(configuration, "storage_for", lambda _: store)
    monkeypatch.setattr(Redis, "from_url", lambda *_a, **_kw: nullcontext(redis))
    result = ProjectStats.model_validate(configuration.project_stats("book"))
    assert result.usage["totals"]["total_tokens"] == (10 if status == "running" else 0)
    assert (result.live is not None) == (status == "running")
    assert store.usage["totals"]["total_tokens"] == 0
