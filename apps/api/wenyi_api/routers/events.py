"""Project event log and timeline endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Query

from ..db import get_pool
from ..project_service import require_project
from ..schemas import EventOut
from ..storage_pg import PostgresStorage

router = APIRouter(prefix="/projects/{pid}/events", tags=["events"])


@router.get("", response_model=list[EventOut])
def list_events(
    pid: str, type: str | None = Query(None), limit: int = Query(200, ge=1, le=1000)
) -> list[dict]:
    require_project(pid)
    storage = PostgresStorage(pid, get_pool())
    rows = storage.list_events(event_type=type, limit=limit)
    out = []
    for r in rows:
        out.append(
            {
                "id": r.get("_id"),
                "type": r.get("event") or r.get("type", ""),
                "payload": {k: v for k, v in r.items() if k not in ("_id", "_ts", "event")},
                "created_at": r.get("_ts"),
            }
        )
    return out
