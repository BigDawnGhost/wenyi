"""Prometheus-compatible metric exposition.

Public on purpose: a scraper holds no API token, and the payload reveals only
counts. Everything else on the API stays behind the token middleware.
"""

from __future__ import annotations

from fastapi import APIRouter, Response

from .. import metrics

router = APIRouter(tags=["meta"])


@router.get("/metrics", response_class=Response)
def exposition() -> Response:
    return Response(metrics.render(), media_type="text/plain; version=0.0.4; charset=utf-8")
