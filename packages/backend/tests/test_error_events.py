"""A failed task must reach live subscribers without leaking failure detail."""

import json

from wenyi_backend.emitters import publish_error
from wenyi_core.events import ERROR_KIND


class RecordingRedis:
    def __init__(self):
        self.published = []

    def publish(self, channel, value):
        self.published.append((channel, json.loads(value)))


class BrokenRedis:
    def publish(self, channel, value):
        raise OSError("offline")


def test_publish_error_sends_a_structured_terminal_event():
    redis = RecordingRedis()
    publish_error(redis, "book", code="pipeline_failed", message="boom", run_id="run-1")
    channel, payload = redis.published[0]
    assert channel == "project:book"
    assert payload["kind"] == ERROR_KIND == "error"
    assert payload["project_id"] == "book"
    assert payload["run_id"] == "run-1"
    assert payload["code"] == "pipeline_failed"
    assert payload["message"] == "boom"


def test_error_kind_is_distinct_from_progress_kinds():
    assert ERROR_KIND not in {"progress", "batch", "chapter", "term", "pipeline", "log"}


def test_transport_failure_is_logged_and_never_raised(caplog):
    with caplog.at_level("WARNING"):
        publish_error(BrokenRedis(), "book", code="pipeline_failed", message="boom")
    warnings = [r for r in caplog.records if r.name == "wenyi_backend.emitters"]
    assert [r.levelname for r in warnings] == ["WARNING"]
    assert "book" in warnings[0].getMessage()
    assert "offline" in warnings[0].getMessage()


def test_run_id_is_optional():
    redis = RecordingRedis()
    publish_error(redis, "book", code="pipeline_failed", message="boom")
    assert redis.published[0][1]["run_id"] is None
