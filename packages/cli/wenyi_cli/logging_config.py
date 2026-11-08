"""Keep CLI diagnostics off live progress output without losing troubleshooting logs."""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import typer
from wenyi_core.observability import configure_logging, use_json_logs


class CliLoggingError(OSError):
    """A diagnostic file could not be opened before running the command."""


@contextmanager
def invocation_logging(config_path: str) -> Iterator[Path]:
    """Install a private file sink for this invocation, then restore its caller's logging."""
    root = logging.getLogger()
    previous, level = list(root.handlers), root.level
    for handler in previous:
        if getattr(handler, "_wenyi_handler", False):
            root.removeHandler(handler)
    directory = Path(config_path).resolve().parent / "logs"
    try:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            descriptor, name = tempfile.mkstemp(
                prefix=f"wenyi-cli-{stamp}-{os.getpid()}-", suffix=".log", dir=directory
            )
            os.close(descriptor)
            path = Path(name)
            configure_logging(service="wenyi-cli", json=use_json_logs(), file_path=path)
        except OSError as error:
            raise CliLoggingError(
                f"Cannot initialize diagnostic log in {directory}: {error}"
            ) from error
        try:
            yield path
        except BaseException as error:
            if not isinstance(error, SystemExit) or error.code not in (None, 0):
                logging.getLogger(__name__).error(
                    "CLI invocation failed", exc_info=not isinstance(error, SystemExit)
                )
                typer.echo(f"Diagnostic log: {path}", err=True)
            raise
    finally:
        for handler in list(root.handlers):
            if handler not in previous and getattr(handler, "_wenyi_handler", False):
                root.removeHandler(handler)
                handler.close()
        added = [handler for handler in root.handlers if handler not in previous]
        root.handlers[:] = [*previous, *added]
        root.setLevel(level)
