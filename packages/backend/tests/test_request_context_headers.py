"""The shared middleware must not duplicate headers the Desktop layer sets."""

import asyncio

from wenyi_backend.request_context import (
    ERROR_ID_HEADER,
    REQUEST_ID_HEADER,
    RequestContextMiddleware,
)


def _scope():
    return {"type": "http", "method": "GET", "path": "/", "headers": []}


def test_headers_are_added_once_when_no_outer_layer_sets_them():
    sent = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 500, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def send(message):
        sent.append(message)

    asyncio.run(RequestContextMiddleware(app)(_scope(), None, send))
    headers = sent[0]["headers"]
    assert [k for k, _ in headers].count(REQUEST_ID_HEADER.encode()) == 1
    assert [k for k, _ in headers].count(ERROR_ID_HEADER.encode()) == 1


def test_inbound_correlation_is_used_verbatim():
    sent = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})

    async def send(message):
        sent.append(message)

    scope = _scope()
    scope["headers"] = [(b"x-request-id", b"trace-42")]
    asyncio.run(RequestContextMiddleware(app)(scope, None, send))
    assert (REQUEST_ID_HEADER.encode(), b"trace-42") in sent[0]["headers"]
    assert all(k != ERROR_ID_HEADER.encode() for k, _ in sent[0]["headers"])
