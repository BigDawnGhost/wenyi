"""Database-pool saturation is transient backpressure: answer 503, not 500."""

from __future__ import annotations

from fastapi.testclient import TestClient
from psycopg_pool import PoolTimeout
from wenyi_api.main import POOL_TIMEOUT_RETRY_AFTER, create_app


def test_pool_timeout_maps_to_503_with_retry_after(web_context):
    app = create_app(context=web_context)

    @app.get("/__test/pool-timeout")
    async def saturated():
        raise PoolTimeout()

    client = TestClient(app)
    response = client.get("/__test/pool-timeout")
    assert response.status_code == 503
    assert response.headers["retry-after"] == POOL_TIMEOUT_RETRY_AFTER
    assert response.json() == {"detail": "Database connection pool is busy; please retry shortly."}
