"""Relay live translation progress from Redis Pub/Sub to WebSocket clients."""

from __future__ import annotations

import asyncio
import hmac
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from redis.asyncio import Redis

from .. import dal
from ..config import settings

router = APIRouter(tags=["ws"])


@router.websocket("/ws/projects/{pid}/progress")
async def project_progress(ws: WebSocket, pid: str) -> None:
    await ws.accept()
    try:
        auth = await asyncio.wait_for(ws.receive_json(), timeout=10)
        if settings.api_token and not hmac.compare_digest(
            str(auth.get("token", "")), settings.api_token
        ):
            await ws.close(code=1008)
            return
        if not dal.get_project(pid):
            await ws.close(code=1008)
            return
    except (asyncio.TimeoutError, ValueError, AttributeError, WebSocketDisconnect):
        await ws.close(code=1008)
        return
    # Send the current state snapshot before forwarding live events.
    try:
        p = dal.get_project(pid) or {}
        chapters = dal.chapter_summaries(pid)
        await ws.send_json({"kind": "snapshot", "project": p, "chapters": chapters})
    except Exception:  # noqa: BLE001
        pass

    redis = Redis.from_url(settings.redis_url)
    pubsub = redis.pubsub()
    await pubsub.subscribe(f"project:{pid}")
    disconnected = asyncio.create_task(ws.receive())
    try:
        while not disconnected.done():
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if msg and msg.get("type") == "message":
                data = msg.get("data")
                if isinstance(data, bytes):
                    data = data.decode("utf-8")
                await ws.send_text(data if isinstance(data, str) else json.dumps(data))
            else:
                # Wait briefly when no progress event is available.
                await asyncio.sleep(0.1)
    except WebSocketDisconnect:
        pass
    finally:
        disconnected.cancel()
        await asyncio.gather(disconnected, return_exceptions=True)
        try:
            await pubsub.unsubscribe(f"project:{pid}")
        except Exception:  # noqa: BLE001
            pass
        await pubsub.aclose()
        await redis.aclose()
