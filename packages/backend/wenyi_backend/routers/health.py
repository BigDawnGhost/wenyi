"""Health check endpoints."""

from __future__ import annotations

from fastapi import APIRouter

from ..context import current_context

router = APIRouter(tags=["meta"])


@router.get("/health")
def health() -> dict:
    try:
        current_context().health()
        db = "ok"
    except Exception as e:  # noqa: BLE001
        db = f"error: {e}"
    return {"status": "ok", "db": db}
