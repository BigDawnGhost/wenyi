"""Request correlation shared by the Web API and the Desktop sidecar."""

from __future__ import annotations

import logging
from uuid import uuid4

from starlette.types import ASGIApp, Message, Receive, Scope, Send
from wenyi_core.observability import bind_request_id, current_request_id, reset_request_id

log = logging.getLogger(__name__)

REQUEST_ID_HEADER = "x-request-id"
ERROR_ID_HEADER = "x-error-id"


def new_request_id() -> str:
    """Return a fresh correlation ID."""
    return uuid4().hex


def request_id_from(scope: Scope) -> str:
    """Reuse an inbound correlation ID when a caller already supplied one."""
    for key, value in scope.get("headers", []):
        if key.lower() == REQUEST_ID_HEADER.encode():
            supplied = value.decode("latin1").strip()
            if supplied:
                return supplied
    return new_request_id()


class RequestContextMiddleware:
    """Correlate a request across response headers and log records.

    The response always carries ``x-request-id``; a 5xx also carries an
    ``x-error-id`` so an operator can find the matching log line without the
    response leaking any failure detail.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = request_id_from(scope)
        scope.setdefault("state", {})["request_id"] = request_id
        token = bind_request_id(request_id)

        async def send_with_ids(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {key.lower() for key, _ in headers}
                # An outer layer such as the Desktop diagnostics middleware may
                # already have stamped this response; never emit a second copy.
                if REQUEST_ID_HEADER.encode() not in present:
                    headers.append((REQUEST_ID_HEADER.encode(), request_id.encode()))
                if message["status"] >= 500 and ERROR_ID_HEADER.encode() not in present:
                    headers.append((ERROR_ID_HEADER.encode(), request_id.encode()))
                message = {**message, "headers": headers}
            await send(message)

        response_started = False

        async def tracked_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send_with_ids(message)

        try:
            await self.app(scope, receive, tracked_send)
        except Exception:
            # Starlette's ServerErrorMiddleware sits outside user middleware, so an
            # unhandled failure would otherwise produce a response we cannot stamp.
            # Emit a correlated 500 here, then re-raise exactly like Starlette does:
            # the server still logs the traceback and a test client can still see
            # the original exception instead of a swallowed, opaque response.
            log.exception(
                "Unhandled error handling request: method=%s path=%s",
                scope.get("method"),
                scope.get("path"),
            )
            if not response_started:
                from starlette.responses import JSONResponse

                response = JSONResponse({"detail": "Internal Server Error"}, status_code=500)
                await response(scope, receive, tracked_send)
            raise
        finally:
            reset_request_id(token)


__all__ = [
    "ERROR_ID_HEADER",
    "REQUEST_ID_HEADER",
    "RequestContextMiddleware",
    "current_request_id",
    "new_request_id",
    "request_id_from",
]
