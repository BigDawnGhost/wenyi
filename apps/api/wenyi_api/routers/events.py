"""项目事件日志（时间线）。"""

from __future__ import annotations

from fastapi import APIRouter, Query

from ..db import get_pool
from ..schemas import EventOut
from ..storage_pg import PostgresStorage

router = APIRouter(prefix="/projects/{pid}/events", tags=["events"])


@router.get("", response_model=list[EventOut])
def list_events(pid: str, type: str | None = Query(None),
                limit: int = Query(200, le=1000)) -> list[dict]:
    storage = PostgresStorage(pid, get_pool())
    rows = storage.list_events(event_type=type, limit=limit)
    out = []
    for r in rows:
        out.append({
            "id": r.get("_id"), "type": r.get("event") or r.get("type", ""),
            "payload": {k: v for k, v in r.items() if k not in ("_id", "_ts", "event")},
            "created_at": r.get("_ts"),
        })
    return out
