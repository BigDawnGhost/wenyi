"""WebSocket progress auth must close with 1008 instead of hanging on bad tokens."""

import asyncio
from types import SimpleNamespace

from wenyi_backend.context import use_context
from wenyi_backend.routers.ws import project_progress


class FakeWebSocket:
    def __init__(self, incoming):
        self.incoming = list(incoming)
        self.accepted = False
        self.closed_with = None

    async def accept(self):
        self.accepted = True

    async def receive_json(self):
        return self.incoming.pop(0)

    async def close(self, code=None):
        self.closed_with = code


def ws_context(relayed, token="secret-token", require_token=True):
    async def relay(socket, pid):
        relayed.append((socket, pid))

    repository = SimpleNamespace(get_project=lambda pid: object())
    telemetry = SimpleNamespace(relay=relay)
    return SimpleNamespace(
        api_token=token,
        require_token=require_token,
        repository=repository,
        telemetry=telemetry,
    )


def run_progress(socket, context):
    with use_context(context):
        asyncio.run(project_progress(socket, "project-1"))


def test_non_ascii_token_closes_with_policy_violation():
    relayed = []
    socket = FakeWebSocket([{"token": "caf\u00e9"}])
    run_progress(socket, ws_context(relayed))
    assert socket.accepted
    assert socket.closed_with == 1008
    assert relayed == []


def test_wrong_ascii_token_closes_with_policy_violation():
    relayed = []
    socket = FakeWebSocket([{"token": "wrong"}])
    run_progress(socket, ws_context(relayed))
    assert socket.closed_with == 1008
    assert relayed == []


def test_valid_token_relays_progress():
    relayed = []
    socket = FakeWebSocket([{"token": "secret-token"}])
    run_progress(socket, ws_context(relayed))
    assert socket.closed_with is None
    assert relayed == [(socket, "project-1")]


def test_wrong_token_relays_progress_when_require_token_disabled():
    relayed = []
    socket = FakeWebSocket([{"token": "wrong"}])
    run_progress(socket, ws_context(relayed, require_token=False))
    assert socket.closed_with is None
    assert relayed == [(socket, "project-1")]
