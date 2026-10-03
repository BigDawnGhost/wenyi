"""Offline OAuth regressions, including real loopback callbacks without external services."""

from __future__ import annotations

import http.client
import socket
import threading
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from wenyi_core.llm.oauth import flows
from wenyi_core.llm.oauth.credentials import OAuthCredentialError


def browser_flow():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    return flows.AuthorizationCodeFlow(
        provider="test",
        display_name="Test",
        client_id="test-client",
        authorization_url="https://login.example/authorize",
        token_url="https://login.example/token",
        scopes=("test",),
        redirect_port=port,
    )


def test_browser_opens_only_after_callback_listener_is_ready(monkeypatch):
    flow = browser_flow()
    posts = []

    def browser(url):
        state = parse_qs(urlsplit(url).query)["state"][0]
        connection = http.client.HTTPConnection(flow.redirect_host, flow.redirect_port, timeout=2)
        connection.request(
            "GET", flow.redirect_path + "?" + urlencode({"state": state, "code": "code"})
        )
        response = connection.getresponse()
        assert response.status == 200
        assert b"Authorization received" in response.read()
        connection.close()
        return True

    def post(url, payload, **kwargs):
        posts.append((url, payload))
        return 200, {"access_token": "test-access", "refresh_token": "test-refresh"}

    monkeypatch.setattr(flows.webbrowser, "open", browser)
    monkeypatch.setattr(flows, "_post", post)
    credential = flows.authorization_code_login(flow, on_authorize=lambda url: None, timeout=2)
    assert credential.access_token == "test-access"
    assert posts[0][1]["code"] == "code"
    assert posts[0][1]["redirect_uri"] == flow.redirect_uri


def test_callback_returns_without_waiting_for_browser_keepalive_to_close():
    flow = browser_flow()
    connections = []
    result = []
    errors = []

    def ready():
        connection = socket.create_connection((flow.redirect_host, flow.redirect_port), timeout=2)
        connections.append(connection)
        connection.sendall(
            (
                f"GET {flow.redirect_path}?state=expected&code=approved HTTP/1.1\r\n"
                "Host: localhost\r\nConnection: keep-alive\r\n\r\n"
            ).encode()
        )
        connection.recv(4096)
        # Deliberately keep the client socket open after receiving the success page.

    def run():
        try:
            result.append(flows.wait_for_redirect(flow, "expected", 2, on_ready=ready))
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        thread.join(3)
        assert not thread.is_alive(), "callback shutdown blocked on the browser socket"
        assert not errors
        assert result == ["approved"]
    finally:
        for connection in connections:
            connection.close()
        thread.join(3)


def test_wrong_state_does_not_consume_the_active_login():
    flow = browser_flow()

    def ready():
        connection = http.client.HTTPConnection(flow.redirect_host, flow.redirect_port, timeout=2)
        connection.request("GET", flow.redirect_path + "?state=wrong&code=ignored")
        response = connection.getresponse()
        assert response.status == 400
        response.read()
        connection.close()
        connection = http.client.HTTPConnection(flow.redirect_host, flow.redirect_port, timeout=2)
        connection.request("GET", flow.redirect_path + "?state=expected&code=approved")
        connection.getresponse().read()
        connection.close()

    assert flows.wait_for_redirect(flow, "expected", 2, on_ready=ready) == "approved"


def test_occupied_callback_port_fails_before_opening_browser(monkeypatch):
    flow = browser_flow()
    with socket.socket() as listener:
        listener.bind((flow.redirect_host, flow.redirect_port))
        listener.listen()
        monkeypatch.setattr(flows.webbrowser, "open", lambda url: pytest.fail("opened browser"))
        with pytest.raises(OAuthCredentialError, match="Cannot listen"):
            flows.authorization_code_login(flow, on_authorize=lambda url: None)


@pytest.mark.parametrize("open_browser", [True, False])
def test_device_login_opens_browser_after_prompt_and_uses_complete_url(monkeypatch, open_browser):
    flow = flows.DeviceCodeFlow(
        provider="test",
        display_name="Test",
        client_id="test-client",
        device_code_url="https://login.example/device",
        token_url="https://login.example/token",
    )
    events = []
    responses = iter(
        [
            (
                200,
                {
                    "device_code": "device",
                    "user_code": "ABCD",
                    "verification_uri": "https://login.example/verify",
                    "verification_uri_complete": "https://login.example/verify?code=ABCD",
                },
            ),
            (200, {"access_token": "test-access"}),
        ]
    )
    monkeypatch.setattr(flows, "_post", lambda *a, **kw: next(responses))
    monkeypatch.setattr(flows.webbrowser, "open", lambda url: events.append(("browser", url)))
    credential = flows.device_code_login(
        flow,
        on_prompt=lambda prompt: events.append(("prompt", prompt.user_code)),
        sleep=lambda seconds: None,
        open_browser=open_browser,
    )
    assert credential.access_token == "test-access"
    assert events[0] == ("prompt", "ABCD")
    assert events[1:] == (
        [("browser", "https://login.example/verify?code=ABCD")] if open_browser else []
    )
