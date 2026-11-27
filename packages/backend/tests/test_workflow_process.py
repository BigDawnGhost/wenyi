"""Process eviction leaves only the previous committed checkpoint available."""

import asyncio
import sys
from contextlib import closing
from unittest.mock import Mock

import pytest
from wenyi_backend.execution import process
from wenyi_backend.execution.contracts import ExecutionDependencies, ExecutionRequest
from wenyi_core.storage.sqlite import SqliteStorage

_BLOCKED_WRITER = """
import threading
from wenyi_backend.execution import process
from wenyi_core.storage.sqlite import SqliteStorage

def write(request, options, send):
    with SqliteStorage(options["root"]).lock():
        store = SqliteStorage(options["root"])
        store.write_artifact("checkpoint.json", {"written": 1})
        with store.state_lock():
            store.write_artifact("checkpoint.json", {"written": 2})
            send({"writing": True})
            threading.Event().wait()

process.run_child(write)
"""


@pytest.mark.parametrize("shutdown", [False, True])
def test_writer_is_killed_before_publishing_terminal_outcome(tmp_path, shutdown):
    root = str(tmp_path / "project")
    projects, jobs, telemetry = Mock(), Mock(), Mock()
    projects.is_paused.return_value = False
    job = {"id": 1, "project_id": "p", "kind": "translation", "status": "queued"}

    def set_status(_identity, status, *, expected_status=None, error=None):
        if expected_status is not None and job["status"] != expected_status:
            return False
        job["status"] = status
        return True

    jobs.get_job_by_arq_id.return_value = job
    jobs.list_jobs.return_value = [job]
    jobs.set_job_status.side_effect = set_status
    dependencies = ExecutionDependencies(
        projects, jobs, lambda _: SqliteStorage(root), telemetry, Mock(), asyncio.to_thread
    )

    async def scenario():
        writing = asyncio.Event()
        task = asyncio.create_task(
            process.run(
                ExecutionRequest("translation", "p", "run"),
                dependencies,
                [sys.executable, "-c", _BLOCKED_WRITER],
                {"root": root},
                receive=lambda frame: writing.set(),
            )
        )
        try:
            await asyncio.wait_for(writing.wait(), 15)
            # A duplicate delivery must not start, wait for, or stop the occupied writer.
            await asyncio.wait_for(
                process.run(
                    ExecutionRequest("translation", "p", "run"),
                    dependencies,
                    ["not-a-valid-executable"],
                    {},
                ),
                2,
            )
            with closing(SqliteStorage(root)) as probe:
                assert probe.read_artifact("checkpoint.json") == {"written": 1}
                with pytest.raises(BlockingIOError), probe.lock(blocking=False):
                    pytest.fail("The live writer must hold the lock")
            if shutdown:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 5)
            else:
                projects.is_paused.return_value = True
                await asyncio.wait_for(task, 5)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    jobs.finish_workflow.assert_called_once_with(
        "p",
        1,
        "interrupted" if shutdown else "paused",
        expected_status="running",
        project_status="paused",
        error=None,
    )
    with closing(SqliteStorage(root)) as probe, probe.lock(blocking=False):
        assert probe.read_artifact("checkpoint.json") == {"written": 1}
