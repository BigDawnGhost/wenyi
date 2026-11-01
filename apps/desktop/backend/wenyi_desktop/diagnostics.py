"""Content-free Desktop observations over the parent's private protocol pipe.

No logging handler, SDK exception, user text or arbitrary output is forwarded.
The native owner validates these records again and owns persistence/retention.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import queue
import secrets
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, TextIO
from uuid import uuid4

EVENTS = frozenset(
    {
        "engine_bootstrap",
        "engine_bootstrap_failed",
        "engine_stopped",
        "task_started",
        "task_completed",
        "task_finished",
        "task_failed",
        "task_timeout",
        "task_cancelled",
        "http_completed",
        "http_mutation",
        "http_failed",
        "http_error",
        "llm_request_started",
        "llm_request_completed",
        "llm_request_failed",
        "llm_retry_wait",
        "llm_retry_exhausted",
        "llm_model_failover",
        "llm_usage",
        "credentials_degraded",
        "logger_dropped",
    }
)
COUNTS = frozenset({"duration_ms", "status_code", "attempt", "wait_ms", "tokens", "count", "line"})
REFERENCES = frozenset({"project_ref", "run_ref", "call_ref"})
IDENTITIES = frozenset({"request_id", "error_id"})
CATEGORIES = frozenset({"timeout", "permission", "io", "invalid", "dependency", "unknown"})
MODULES = frozenset(
    {"desktop", "main", "local_runtime", "tasks", "router", "retrying", "translation", "sqlite"}
)
PHASES = frozenset(
    {
        "language",
        "analysis",
        "synopsis",
        "translation",
        "polish",
        "glossary",
        "terminology",
        "annotation",
        "review",
        "autofix",
        "srt",
    }
)
KINDS = frozenset(
    {"parse", "prepare", "translation", "chapter_translation", "review", "srt", "export"}
)
_scope: ContextVar[tuple[Reporter | None, dict[str, str]]] = ContextVar(
    "desktop_diagnostics", default=(None, {})
)


def project_attributes(data: dict[str, Any]) -> dict:
    """Allow only fixed classifications, bounded integers and opaque identities."""
    result = {}
    for key, value in data.items():
        if key in COUNTS and type(value) is int and 0 <= value <= 2**53 - 1:
            result[key] = value
        elif key in REFERENCES | IDENTITIES and isinstance(value, str):
            if len(value) == 32 and all(char in "0123456789abcdef" for char in value):
                result[key] = value
        elif key == "category" and isinstance(value, str) and value in CATEGORIES:
            result[key] = value
        elif key == "module" and isinstance(value, str) and value in MODULES:
            result[key] = value
        elif key == "phase" and isinstance(value, str) and value in PHASES:
            result[key] = value
        elif key == "kind" and isinstance(value, str) and value in KINDS:
            result[key] = value
    return result


def failure_attributes(error: BaseException) -> dict:
    """Never stringify an exception or retain its traceback/locals."""
    category = (
        "timeout"
        if isinstance(error, TimeoutError)
        else "permission"
        if isinstance(error, PermissionError)
        else "io"
        if isinstance(error, OSError)
        else "invalid"
        if isinstance(error, ValueError)
        else "dependency"
        if isinstance(error, ImportError)
        else "unknown"
    )
    result: dict = {"category": category}
    tb = error.__traceback__
    while tb is not None:
        name = tb.tb_frame.f_globals.get("__name__", "")
        if isinstance(name, str) and name.startswith(
            ("wenyi_desktop.", "wenyi_backend.", "wenyi_core.")
        ):
            module = name.rsplit(".", 1)[-1]
            if module in MODULES:
                result.update(module=module, line=tb.tb_lineno)
        tb = tb.tb_next
    return result


class Reporter:
    """Bounded producer; pipe stalls never block a task or a state transaction."""

    def __init__(self, stream: TextIO, *, capacity: int = 4096):
        self.stream = stream
        self.salt = secrets.token_bytes(32)
        self.queue: queue.Queue[str | None] = queue.Queue(maxsize=capacity)
        self.dropped = 0
        self.lock = threading.Lock()
        self.closed = False
        self.writer = threading.Thread(target=self._write, name="desktop-diagnostics", daemon=True)
        self.writer.start()

    def reference(self, value: str) -> str:
        return hmac.new(self.salt, value.encode("utf-8"), hashlib.sha256).hexdigest()[:32]

    def emit(self, event: str, **data: Any) -> None:
        if event not in EVENTS or self.closed:
            return
        safe = json.dumps(
            {"event": event, "attributes": project_attributes(data)}, separators=(",", ":")
        )
        try:
            self.queue.put_nowait("WENYI_DIAGNOSTIC " + safe + "\n")
        except queue.Full:
            with self.lock:
                self.dropped += 1

    def _write(self) -> None:
        while True:
            value = self.queue.get()
            try:
                if value is None:
                    return
                with self.lock:
                    dropped, self.dropped = self.dropped, 0
                if dropped:
                    self.stream.write(
                        'WENYI_DIAGNOSTIC {"event":"logger_dropped","attributes":{"count":'
                        + str(dropped)
                        + "}}\n"
                    )
                self.stream.write(value)
                self.stream.flush()
            except (OSError, ValueError):
                # The parent is gone. Never fall back to arbitrary console logging.
                self.closed = True
                return
            finally:
                self.queue.task_done()

    def protocol(self, value: str) -> None:
        """Serialize readiness with diagnostics so concurrent writes cannot interleave."""
        self.queue.put(value, timeout=2)

    def close(self) -> None:
        self.closed = True
        try:
            self.queue.put(None, timeout=0.1)
        except queue.Full:
            return
        self.writer.join(timeout=2)


@contextmanager
def scope(reporter: Reporter | None, **references: str):
    attributes = {
        key: reporter.reference(value)
        for key, value in references.items()
        if reporter is not None and key in REFERENCES and isinstance(value, str)
    }
    token = _scope.set((reporter, attributes))
    try:
        yield
    finally:
        _scope.reset(token)


def current() -> tuple[Reporter | None, dict[str, str]]:
    return _scope.get()


def emit(event: str, **attributes: Any) -> None:
    reporter, context = current()
    if reporter is not None:
        reporter.emit(event, **{**context, **attributes})


class DiagnosticsMiddleware:
    """Desktop-only ASGI observations without request paths, headers or bodies."""

    def __init__(self, app, reporter: Reporter | None, origins: tuple[str, ...] = ()):
        self.app = app
        self.reporter = reporter
        self.origins = origins

    async def __call__(self, asgi_scope, receive, send):
        if asgi_scope["type"] != "http" or self.reporter is None:
            await self.app(asgi_scope, receive, send)
            return
        started = time.monotonic()
        request_id = uuid4().hex
        status = 500
        error_id = None
        response_started = False

        async def observe(message):
            nonlocal status, error_id, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status = message["status"]
                headers = list(message.get("headers", []))
                present = {key.lower() for key, _ in headers}
                # The shared request-context middleware runs inside this layer and
                # may already have stamped the response; keep a single copy.
                if b"x-request-id" not in present:
                    headers.append((b"x-request-id", request_id.encode()))
                if status >= 500:
                    error_id = error_id or uuid4().hex
                    if b"x-error-id" not in present:
                        headers.append((b"x-error-id", error_id.encode()))
                # Merge with CORS rather than replace its Content-Disposition exposure.
                exposed = [
                    value
                    for key, value in headers
                    if key.lower() == b"access-control-expose-headers"
                ]
                headers = [
                    (key, value)
                    for key, value in headers
                    if key.lower() != b"access-control-expose-headers"
                ]
                headers.append(
                    (
                        b"access-control-expose-headers",
                        b", ".join([*exposed, b"X-Request-ID", b"X-Error-ID"]),
                    )
                )
                message = {**message, "headers": headers}
            await send(message)

        fields: dict = {"request_id": request_id}
        try:
            with scope(self.reporter):
                await self.app(asgi_scope, receive, observe)
        except Exception as error:
            fields.update(failure_attributes(error))
            error_id = error_id or uuid4().hex
            if response_started:
                raise
            # Own unexpected failures before headers: return the same safe ID that we record.
            # Never stringify the exception or let the generic server handler lose correlation.
            from starlette.responses import JSONResponse

            response = JSONResponse(
                {
                    "detail": "The local operation failed. Export a diagnostic report.",
                    "code": "desktop_operation_failed",
                    "error_id": error_id,
                },
                status_code=500,
            )
            origin = dict(asgi_scope.get("headers", [])).get(b"origin", b"").decode("latin1")
            if origin in self.origins:
                response.headers["Access-Control-Allow-Origin"] = origin
                response.headers["Vary"] = "Origin"
            await response(asgi_scope, receive, observe)
        finally:
            fields.update(status_code=status, duration_ms=int((time.monotonic() - started) * 1000))
            if error_id is not None:
                fields["error_id"] = error_id
            event = (
                "http_error"
                if status >= 500
                else "http_failed"
                if status >= 400
                else "http_completed"
                if asgi_scope.get("method") in {"GET", "HEAD", "OPTIONS"}
                else "http_mutation"
            )
            self.reporter.emit(event, **fields)
