"""Authenticate progress subscriptions before handing off to the telemetry port."""

from __future__ import annotations

import asyncio
import hmac

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .. import dal
from ..context import current_context

router = APIRouter(tags=["ws"])


@router.websocket("/ws/projects/{pid}/progress")
async def project_progress(ws: WebSocket, pid: str) -> None:
    await ws.accept()
    try:
        auth = await asyncio.wait_for(ws.receive_json(), timeout=10)
        token = current_context().api_token
        if token and not hmac.compare_digest(str(auth.get("token", "")), token):
            await ws.close(code=1008)
            return
        if not await asyncio.to_thread(dal.get_project, pid):
            await ws.close(code=1008)
            return
    except (asyncio.TimeoutError, ValueError, AttributeError, WebSocketDisconnect):
        await ws.close(code=1008)
        return
    await current_context().telemetry.relay(ws, pid)
