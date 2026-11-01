"""Redis counters shared by Web API and worker processes in the same deployment."""

from __future__ import annotations

import json

from redis import Redis
from wenyi_backend.metrics import MAX_SEEN, Counters, Metric

# Deduplication and increment must commit together, including concurrent reports.
# A bounded FIFO retains recent identities without periodically clearing all of them.
_INCREMENT = """
if ARGV[2] ~= '' and redis.call('SISMEMBER', KEYS[2], ARGV[2]) == 1 then
    return 0
end
redis.call('HINCRBY', KEYS[1], ARGV[1], 1)
if ARGV[2] ~= '' then
    redis.call('SADD', KEYS[2], ARGV[2])
    redis.call('RPUSH', KEYS[3], ARGV[2])
    if redis.call('LLEN', KEYS[3]) > tonumber(ARGV[3]) then
        redis.call('SREM', KEYS[2], redis.call('LPOP', KEYS[3]))
    end
end
return 1
"""


class RedisCounters:
    def __init__(
        self, url: str, *, namespace: str = "wenyi:metrics", max_seen: int = MAX_SEEN
    ) -> None:
        self.url = url
        self.namespace = namespace
        self.max_seen = max_seen

    def _connect(self):
        return Redis.from_url(
            self.url, socket_timeout=1, socket_connect_timeout=1, decode_responses=True
        )

    def increment(self, metric: Metric, labels: tuple[str, str], identity: str | None) -> None:
        with self._connect() as cache:
            cache.eval(
                _INCREMENT,
                3,
                f"{self.namespace}:{metric}",
                f"{self.namespace}:seen",
                f"{self.namespace}:order",
                json.dumps(labels),
                f"{metric}:{identity}" if identity is not None else "",
                str(self.max_seen),
            )

    def snapshot(self) -> Counters:
        with self._connect() as cache, cache.pipeline() as transaction:
            transaction.hgetall(f"{self.namespace}:tasks")
            transaction.hgetall(f"{self.namespace}:retries")
            tasks, retries = transaction.execute()
        counts: Counters = {"tasks": {}, "retries": {}}
        for metric, rows in (("tasks", tasks), ("retries", retries)):
            for key, value in rows.items():
                first, second = json.loads(key)
                counts[metric][first, second] = int(value)
        return counts
