"""Desktop responses must expose exactly one correlation ID per header name."""

import asyncio

from wenyi_backend.request_context import RequestContextMiddleware
from wenyi_desktop.diagnostics import DiagnosticsMiddleware, Reporter


def test_desktop_layering_does_not_duplicate_correlation_headers():
    import io

    stream = io.StringIO()
    reporter = Reporter(stream)
    sent = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 500, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    inner = RequestContextMiddleware(app)
    outer = DiagnosticsMiddleware(inner, reporter)

    async def send(message):
        sent.append(message)

    asyncio.run(outer({"type": "http", "method": "GET", "path": "/", "headers": []}, None, send))
    reporter.close()
    keys = [key for key, _ in sent[0]["headers"]]
    assert keys.count(b"x-request-id") == 1, keys
    assert keys.count(b"x-error-id") == 1, keys
