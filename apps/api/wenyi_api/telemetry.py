"""Redis cache and WebSocket transport."""

import asyncio
import inspect
import json

from fastapi import WebSocketDisconnect
from redis import Redis
from redis.asyncio import Redis as AsyncRedis
from wenyi_backend.live_statistics import read_live_statistics
from wenyi_backend.ports.projects import ProjectQueries

# A run can legitimately publish nothing for minutes (one long model call), and
# an idle socket is indistinguishable from a dead one from the client's side.
# The frame is the only thing that proves the link is still carrying data.
HEARTBEAT_SECONDS = 30.0
HEARTBEAT_FRAME = json.dumps({"kind": "heartbeat"})


class RedisTelemetry:
    def __init__(self, url: str, projects: ProjectQueries):
        self.url = url
        self.projects = projects

    def connect(self):
        return Redis.from_url(self.url, socket_timeout=1, socket_connect_timeout=1)

    def release(self, cache):
        cache.close()

    def progress_snapshot(self, pid):
        with self.connect() as cache:
            raw = cache.get(f"project:{pid}:progress")
        # redis-py's shared command annotation includes asyncio responses, but
        # this cache uses redis.Redis, not redis.asyncio.Redis.
        assert not inspect.isawaitable(raw), "Synchronous Redis GET returned an awaitable"
        return json.loads(raw) if raw else None

    def statistics_snapshot(self, pid, job):
        with self.connect() as cache:
            return read_live_statistics(cache, pid, job)

    async def relay(self, websocket, pid):
        redis = AsyncRedis.from_url(self.url)
        pubsub = redis.pubsub()
        disconnected = None
        try:
            await pubsub.subscribe(f"project:{pid}")
            project = await asyncio.to_thread(self.projects.get_project, pid)
            chapters = await asyncio.to_thread(self.projects.chapter_summaries, pid)
            await websocket.send_json(
                {"kind": "snapshot", "project": project or {}, "chapters": chapters}
            )
            disconnected = asyncio.create_task(websocket.receive())
            loop = asyncio.get_running_loop()
            last_sent = loop.time()
            while not disconnected.done():
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message and message.get("type") == "message":
                    data = message.get("data")
                    if isinstance(data, bytes):
                        data = data.decode("utf-8")
                    await websocket.send_text(data if isinstance(data, str) else json.dumps(data))
                    last_sent = loop.time()
                else:
                    if loop.time() - last_sent >= HEARTBEAT_SECONDS:
                        await websocket.send_text(HEARTBEAT_FRAME)
                        last_sent = loop.time()
                    await asyncio.sleep(0.1)
        except WebSocketDisconnect:
            pass
        finally:
            if disconnected:
                disconnected.cancel()
                await asyncio.gather(disconnected, return_exceptions=True)
            await pubsub.aclose()
            await redis.aclose()
