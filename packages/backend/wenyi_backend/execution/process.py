"""Supervise an expendable workflow writer without abandoning its project lock."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict
from typing import Any

from wenyi_core.errors import WenyiError

from . import runner
from .contracts import ExecutionDependencies, ExecutionRequest

log = logging.getLogger(__name__)
_POLL_SECONDS = 0.1
_OUTPUT_LIMIT = 1 << 20


async def run(
    request: ExecutionRequest,
    dependencies: ExecutionDependencies,
    command: list[str],
    options: dict[str, Any],
    *,
    receive: Callable[[dict], None] | None = None,
    stderr: int | None = None,
    env: dict[str, str] | None = None,
    pause_requested: asyncio.Event | None = None,
) -> None:
    """A normal completion comes from the child; only its supervisor may force a stop."""
    if not request.run_id:
        raise ValueError("An isolated workflow requires a durable run identity")

    async def wait_for_pause() -> None:
        while True:
            if pause_requested is not None and pause_requested.is_set():
                return
            try:
                if await asyncio.to_thread(dependencies.projects.is_paused, request.project_id):
                    return
            except Exception:
                # Retry the initial read if a pause may have committed before registration.
                pass
            else:
                if pause_requested is not None:
                    await pause_requested.wait()
                    return
            await asyncio.sleep(_POLL_SECONDS)

    async def supervise() -> None:
        if not await asyncio.to_thread(runner.claim, request, dependencies):
            return
        try:
            child = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=stderr,
                env=env,
                limit=_OUTPUT_LIMIT,
            )
        except OSError as error:
            failure = WenyiError("Could not start the workflow process")
            await asyncio.to_thread(
                runner.publish_outcome,
                request,
                dependencies,
                runner.WorkflowOutcome("error", error=failure),
            )
            raise failure from error
        assert child.stdin is not None and child.stdout is not None
        stdin, stdout = child.stdin, child.stdout

        result: dict | None = None

        async def read_output() -> None:
            nonlocal result
            while line := await stdout.readline():
                try:
                    frame = json.loads(line)
                    if "result" in frame:
                        result = frame["result"]
                    elif receive is not None:
                        receive(frame)
                except Exception:
                    log.warning("Could not relay workflow output", exc_info=True)

        output = asyncio.create_task(read_output())
        exited = asyncio.create_task(child.wait())
        pausing = asyncio.create_task(wait_for_pause())
        closing = asyncio.create_task(stop.wait())
        outcome = None
        try:
            stdin.write(json.dumps({"request": asdict(request), **options}).encode() + b"\n")
            done, _ = await asyncio.wait(
                (exited, pausing, closing), return_when=asyncio.FIRST_COMPLETED
            )
            if closing in done:
                outcome = runner.WorkflowOutcome("interrupted", project_status="paused")
            elif pausing in done:
                outcome = runner.WorkflowOutcome("paused")
        except (BrokenPipeError, ConnectionResetError):
            pass  # The child can exit before the input arrives.
        finally:
            if not exited.done():
                # Never wait for the writer to save a last checkpoint.
                with suppress(ProcessLookupError):
                    child.kill()
            stdin.close()
            pausing.cancel()
            closing.cancel()
            await asyncio.gather(exited, output, pausing, closing, return_exceptions=True)

        if outcome is None and child.returncode == 0 and result:
            outcome = runner.WorkflowOutcome.from_record(result)
        if outcome is None and (child.returncode != 0 or result is None):
            error = WenyiError("Workflow process stopped; resume to continue from saved progress")
            outcome = runner.WorkflowOutcome("interrupted", project_status="paused", error=error)
        if outcome is not None:
            published = await asyncio.to_thread(
                runner.publish_outcome,
                request,
                dependencies,
                outcome,
                forced=outcome.status in {"interrupted", "paused"} and not result,
            )
            if published and published.error is not None and published.status != "paused":
                raise published.error

    stop = asyncio.Event()
    owned = asyncio.create_task(supervise())
    try:
        await asyncio.shield(owned)
    except asyncio.CancelledError:
        stop.set()
        await runner.drain(owned)
        raise


def run_child(
    entry: Callable[
        [ExecutionRequest, dict, Callable[[dict], None]], runner.WorkflowOutcome | None
    ],
) -> None:
    """Read private launch data and exit even if the supervisor disappears abruptly."""
    wire = sys.stdout.buffer
    sys.stdout = sys.stderr
    data = json.loads(sys.stdin.buffer.readline())
    request = ExecutionRequest(**data.pop("request"))

    def watch_owner() -> None:
        # Do not hold Python's buffered stdin lock across interpreter shutdown.
        os.read(sys.stdin.fileno(), 1)
        os._exit(1)

    threading.Thread(target=watch_owner, daemon=True, name="workflow-owner").start()
    lock = threading.Lock()

    def send(event: dict) -> None:
        line = json.dumps(event, ensure_ascii=True).encode() + b"\n"
        if len(line) < _OUTPUT_LIMIT:
            with lock:
                wire.write(line)
                wire.flush()

    try:
        outcome = entry(request, data, send)
    except BaseException as error:
        log.exception("Workflow process failed: run_id=%s", request.run_id)
        outcome = runner.WorkflowOutcome("error", error=error)
    send({"result": outcome.as_record() if outcome is not None else {}})
