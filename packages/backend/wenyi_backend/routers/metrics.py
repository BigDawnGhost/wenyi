"""Prometheus-compatible snapshots, protected by the application's usual authentication."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response

from .. import metrics
from ..context import current_context

router = APIRouter(tags=["meta"])


@router.get("/metrics", response_class=Response)
def exposition() -> Response:
    context = current_context()
    try:
        text = metrics.render(
            context.metrics.snapshot(), depth=context.repository.queued_job_count()
        )
    except Exception:
        raise HTTPException(503, "Metrics are unavailable; retry the request") from None
    return Response(text, media_type="text/plain; version=0.0.4; charset=utf-8")
