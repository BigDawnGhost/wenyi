"""WebSocket：实时翻译进度（订阅 Redis Pub/Sub channel）。"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from redis.asyncio import Redis

from .. import dal
from ..config import settings

router = APIRouter(tags=["ws"])


@router.websocket("/ws/projects/{pid}/progress")
async def project_progress(ws: WebSocket, pid: str) -> None:
    await ws.accept()
    # 先发一次状态快照
    try:
        p = dal.get_project(pid) or {}
        chapters = dal.chapter_summaries(pid)
        await ws.send_json({"kind": "snapshot", "project": p, "chapters": chapters})
    except Exception:  # noqa: BLE001
        pass

    redis = Redis.from_url(settings.redis_url)
    pubsub = redis.pubsub()
    await pubsub.subscribe(f"project:{pid}")
    try:
        while True:
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if msg and msg.get("type") == "message":
                data = msg.get("data")
                if isinstance(data, bytes):
                    data = data.decode("utf-8")
                await ws.send_text(data if isinstance(data, str) else json.dumps(data))
            else:
                # 顺带把暂停态反馈给前端（轻量心跳）
                await asyncio.sleep(0.1)
    except WebSocketDisconnect:
        pass
    finally:
        try:
            await pubsub.unsubscribe(f"project:{pid}")
        except Exception:  # noqa: BLE001
            pass
        await redis.close()
