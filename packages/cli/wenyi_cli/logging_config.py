"""Keep CLI diagnostics off live progress output without losing troubleshooting logs."""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import typer
from wenyi_core.observability import bind_run_id, configure_logging, reset_run_id, use_json_logs


class CliLoggingError(OSError):
    """A diagnostic file could not be opened before running the command."""


def invocation_log_dir(config_path: str) -> Path:
    """Return the diagnostic log directory inside the selected project data directory.

    The CLI data directory is the folder holding the selected configuration file
    (next to the ``state/`` tree), so diagnostics always land in ``<data-dir>/logs/``
    instead of loose files in the working directory.
    """
    return Path(config_path).resolve().parent / "logs"


@contextmanager
def invocation_logging(config_path: str) -> Iterator[Path]:
    """Install a private file sink for this invocation, then restore its caller's logging.

    The invocation binds a fresh run ID that is attached to every emitted record so
    console and file lines can be correlated and grepped afterwards.
    """
    root = logging.getLogger()
    previous, level = list(root.handlers), root.level
    for handler in previous:
        if getattr(handler, "_wenyi_handler", False):
            root.removeHandler(handler)
    directory = invocation_log_dir(config_path)
    run_token = bind_run_id(uuid4().hex)
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
        reset_run_id(run_token)
        for handler in list(root.handlers):
            if handler not in previous and getattr(handler, "_wenyi_handler", False):
                root.removeHandler(handler)
                handler.close()
        added = [handler for handler in root.handlers if handler not in previous]
        root.handlers[:] = [*previous, *added]
        root.setLevel(level)
