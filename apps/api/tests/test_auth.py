"""Authentication and CORS must compose for separately hosted Web clients."""

from dataclasses import replace

from fastapi.testclient import TestClient
from wenyi_api import main


def test_authenticated_api_accepts_preflight_without_exposing_protected_routes(monkeypatch):
    monkeypatch.setattr(main, "settings", replace(main.settings, api_token="test-token"))
    monkeypatch.setenv("WENYI_CORS_ORIGINS", "https://web.example")
    application = main.create_app()

    @application.get("/protected")
    def protected():
        return {"ok": True}

    # Do not enter the lifespan: this test requires no database.
    client = TestClient(application)
    response = client.options(
        "/protected",
        headers={
            "Origin": "https://web.example",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://web.example"

    response = client.get("/protected", headers={"Origin": "https://web.example"})
    assert response.status_code == 401
    assert response.headers["access-control-allow-origin"] == "https://web.example"

    response = client.get(
        "/protected",
        headers={"Origin": "https://web.example", "Authorization": "Bearer test-token"},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}

    response = client.options(
        "/protected",
        headers={
            "Origin": "https://untrusted.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


def test_chapter_title_edit_uses_api_authentication(monkeypatch):
    monkeypatch.setattr(main, "settings", replace(main.settings, api_token="test-token"))
    client = TestClient(main.create_app())
    try:
        path = "/projects/test/chapters/0/title"
        assert client.put(path, json={}).status_code == 401
        assert (
            client.put(path, json={}, headers={"Authorization": "Bearer wrong"}).status_code == 401
        )
        # Valid credentials reach body validation without needing a database.
        assert (
            client.put(path, json={}, headers={"Authorization": "Bearer test-token"}).status_code
            == 422
        )
    finally:
        client.close()
