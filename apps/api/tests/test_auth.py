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


def test_default_wildcard_origins_do_not_allow_credentials(monkeypatch):
    monkeypatch.delenv("WENYI_CORS_ORIGINS", raising=False)
    application = main.create_app()
    client = TestClient(application)
    try:
        response = client.options(
            "/health",
            headers={
                "Origin": "https://anything.example",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "*"
        # Audit C-1: wildcard origins are untrusted, so credentials must stay disabled.
        assert response.headers.get("access-control-allow-credentials") != "true"
    finally:
        client.close()


def test_explicit_origins_allow_credentials(monkeypatch):
    monkeypatch.setenv("WENYI_CORS_ORIGINS", "https://web.example")
    application = main.create_app()
    client = TestClient(application)
    try:
        response = client.options(
            "/health",
            headers={
                "Origin": "https://web.example",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-credentials"] == "true"
    finally:
        client.close()


def test_require_token_env_flag_parses_fail_closed(monkeypatch):
    from wenyi_api.config import _env_bool

    monkeypatch.delenv("WENYI_REQUIRE_TOKEN", raising=False)
    assert _env_bool("WENYI_REQUIRE_TOKEN", True) is True

    for off_value in ("0", "false", "False", "FALSE", "no", "No", "off", "OFF", " off "):
        monkeypatch.setenv("WENYI_REQUIRE_TOKEN", off_value)
        assert _env_bool("WENYI_REQUIRE_TOKEN", True) is False, off_value

    # Anything else, including a typo, must fail closed and keep auth enabled.
    for on_value in ("1", "true", "yes", "on", "enable", "maybe", ""):
        monkeypatch.setenv("WENYI_REQUIRE_TOKEN", on_value)
        assert _env_bool("WENYI_REQUIRE_TOKEN", True) is True, on_value


def test_require_token_opt_out_disables_bearer_enforcement(monkeypatch):
    monkeypatch.setattr(
        main,
        "settings",
        replace(main.settings, api_token="test-token", require_token=False),
    )
    application = main.create_app()

    @application.get("/protected")
    def protected():
        return {"ok": True}

    # Do not enter the lifespan: this test requires no database.
    client = TestClient(application)
    try:
        assert client.get("/protected").status_code == 200
    finally:
        client.close()


def test_require_token_defaults_stay_enforced(monkeypatch):
    # Without an explicit opt-out the token check must stay on (fail-closed).
    monkeypatch.setattr(main, "settings", replace(main.settings, api_token="test-token"))
    application = main.create_app()

    @application.get("/protected")
    def protected():
        return {"ok": True}

    client = TestClient(application)
    try:
        assert client.get("/protected").status_code == 401
    finally:
        client.close()


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
