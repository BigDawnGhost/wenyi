"""Publish core progress callbacks through the injected telemetry cache port."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from wenyi_core.events import TranslationEvent, make_progress_fn

from .live_statistics import StatisticsCache


class RedisEmitter:
    """Publish to ``project:{id}`` through Redis or the local progress hub."""

    def __init__(self, redis: StatisticsCache, project_id: str, run_id: str | None = None):
        self.run_id = run_id
        self._redis = redis
        self._project_id = project_id
        self.channel = f"project:{project_id}"
        self._started = time.monotonic()

    def emit(self, event: TranslationEvent) -> None:
        payload = {
            "run_id": self.run_id,
            "project_id": event.project_id or self._project_id,
            "kind": event.kind,
            "done": event.done,
            "total": event.total,
            "label": event.label,
            "payload": event.payload,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": max(0.0, time.monotonic() - self._started),
        }
        try:
            encoded = json.dumps(payload, ensure_ascii=False)
            self._redis.set(f"{self.channel}:progress", encoded, ex=604800)
            self._redis.publish(self.channel, encoded)
        except Exception:
            # Redis failures must not interrupt translation; persisted events remain available.
            return None


def redis_progress_fn(
    redis: StatisticsCache, project_id: str, *, kind: str = "progress", run_id: str | None = None
):
    """Build a core progress callback for the configured telemetry transport."""
    return make_progress_fn(RedisEmitter(redis, project_id, run_id), project_id, kind=kind)
