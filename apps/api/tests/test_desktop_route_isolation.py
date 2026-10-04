"""The Web application never mounts Desktop-only routes."""

from fastapi.testclient import TestClient
from wenyi_api.config import Settings
from wenyi_api.main import create_app


def test_desktop_export_routes_are_absent_in_web_app():
    assert not any(
        path.startswith("/desktop/projects/") for path in create_app().openapi()["paths"]
    )


def test_desktop_credentials_route_is_absent_in_web_app():
    # Route matching needs no database or application lifespan.
    client = TestClient(create_app(Settings(api_token=None)))
    assert client.get("/desktop/credentials").status_code == 404
