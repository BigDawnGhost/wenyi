"""健康检查。"""

from __future__ import annotations

from fastapi import APIRouter

from ..db import get_pool

router = APIRouter(tags=["meta"])


@router.get("/health")
def health() -> dict:
    try:
        with get_pool().connection() as c:
            c.execute("SELECT 1")
        db = "ok"
    except Exception as e:  # noqa: BLE001
        db = f"error: {e}"
    return {"status": "ok", "db": db}
