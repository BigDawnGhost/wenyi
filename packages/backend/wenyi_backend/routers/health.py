"""Health check endpoints."""

from __future__ import annotations

from fastapi import APIRouter

from ..context import current_context

router = APIRouter(tags=["meta"])


@router.get("/health")
def health() -> dict:
    """Report liveness, and whether the database answered.

    The endpoint is unauthenticated, so a driver exception must never reach the body: it
    echoes hosts, ports and file paths to anyone who asks. The failure is reported as a
    fixed category instead, and the status stays 200 because the documented use is a
    proxy diagnostic where a non-200 means the API container itself is down.
    """
    try:
        current_context().health()
    except Exception:  # noqa: BLE001 - the probe is best-effort by contract
        return {"status": "ok", "db": "unavailable"}
    return {"status": "ok", "db": "ok"}
