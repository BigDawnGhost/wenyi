"""Process-wide logging assembly shared by the CLI, HTTP API and Desktop sidecar."""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
from typing import Any

# Carries the correlation ID for the request or task currently being served.
_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "wenyi_request_id", default=None
)

HUMAN_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
JSON_LOG_FORMAT_ENV = "WENYI_LOG_FORMAT"


def current_request_id() -> str | None:
    """Return the request ID bound to the current context, if any."""
    return _request_id.get()


def bind_request_id(request_id: str) -> contextvars.Token:
    """Bind a request ID for the current context and return its reset token."""
    return _request_id.set(request_id)


def reset_request_id(token: contextvars.Token) -> None:
    """Restore the request ID binding that preceded :func:`bind_request_id`."""
    _request_id.reset(token)


class _ContextFilter(logging.Filter):
    """Attach the ambient request ID and service name to every record."""

    def __init__(self, service: str | None = None) -> None:
        super().__init__()
        self.service = service

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "request_id", None):
            record.request_id = current_request_id() or "-"
        if self.service and not getattr(record, "service", None):
            record.service = self.service
        return True


class JsonFormatter(logging.Formatter):
    """Render one JSON object per line so log shippers can parse output directly."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        service = getattr(record, "service", None)
        if service:
            payload["service"] = service
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def use_json_logs(json_logs: bool | None = None) -> bool:
    """Resolve the output format, letting an explicit argument win over the environment."""
    if json_logs is not None:
        return json_logs
    return os.environ.get(JSON_LOG_FORMAT_ENV, "").strip().lower() == "json"


def configure_logging(
    level: str = "INFO",
    *,
    json: bool | None = None,
    service: str | None = None,
    stream: Any | None = None,
) -> None:
    """Install process logging; safe to call again when an entry point restarts.

    Only handlers this function installed are replaced. A handler added by
    someone else — a test runner's capture handler, for example — is kept so
    reconfiguring never silences existing output.
    """
    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, "_wenyi_handler", False):
            root.removeHandler(existing)

    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(
        JsonFormatter() if use_json_logs(json) else logging.Formatter(HUMAN_FORMAT)
    )
    handler.addFilter(_ContextFilter(service))
    handler._wenyi_handler = True
    root.addHandler(handler)
    root.setLevel(level.upper())
