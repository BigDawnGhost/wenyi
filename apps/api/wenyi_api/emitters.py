"""进度事件发射器：把内核的 ProgressFn 调用发布到 Redis Pub/Sub。"""

from __future__ import annotations

import json

from redis import Redis
from wenyi_core.events import TranslationEvent, make_progress_fn


class RedisEmitter:
    """发布到 Redis channel ``project:{id}``，供 WebSocket 中继转发。"""

    def __init__(self, redis: Redis, project_id: str):
        self._redis = redis
        self._project_id = project_id
        self.channel = f"project:{project_id}"

    def emit(self, event: TranslationEvent) -> None:
        payload = {
            "project_id": event.project_id or self._project_id,
            "kind": event.kind,
            "done": event.done,
            "total": event.total,
            "label": event.label,
            "payload": event.payload,
        }
        try:
            self._redis.publish(self.channel, json.dumps(payload, ensure_ascii=False))
        except Exception:
            # Redis 不可用不应阻断翻译；静默降级（事件仍在 events 表里）。
            return None


def redis_progress_fn(redis: Redis, project_id: str, *, kind: str = "progress"):
    """构造内核 ProgressFn：done/total/label → Redis 发布。"""
    return make_progress_fn(RedisEmitter(redis, project_id), project_id, kind=kind)
