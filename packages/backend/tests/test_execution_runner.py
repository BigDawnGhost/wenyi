"""Explicit execution ports preserve cleanup, identity and publication lock ownership."""

import asyncio
import json
import threading
from contextlib import closing, contextmanager, nullcontext
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from wenyi_backend.emitters import redis_progress_fn
from wenyi_backend.execution import exporting, runner
from wenyi_backend.execution.contracts import ExecutionDependencies, ExecutionRequest, ExportRequest
from wenyi_core.events import emit_progress
from wenyi_core.storage.sqlite import SqliteStorage


@pytest.fixture
def execution(monkeypatch):
    storage = Mock()
    storage.lock.side_effect = lambda **kwargs: nullcontext()
    projects = Mock()
    projects.is_paused.return_value = False
    projects.get_project.return_value = {"id": "project", "status": "translating"}
    jobs = Mock()
    job = {
        "id": 1,
        "project_id": "project",
        "run_id": "run",
        "kind": "translation",
        "status": "queued",
    }
    jobs.get_job_by_arq_id.return_value = job
    jobs.list_jobs.return_value = [job]
    telemetry = Mock()
    operation = Mock(return_value="done")
    dependencies = ExecutionDependencies(
        projects, jobs, lambda _: storage, telemetry, operation, asyncio.to_thread
    )
    monkeypatch.setattr(
        runner, "LiveStatistics", lambda *args: nullcontext(SimpleNamespace(bind_client=Mock()))
    )
    monkeypatch.setattr(runner, "redis_progress_fn", lambda *args, **kwargs: Mock())
    return ExecutionRequest("translation", "project", "run"), dependencies, storage


def run_workflow(request, dependencies):
    asyncio.run(runner.run(request, dependencies))


def test_runner_uses_supplied_ports_not_ambient_repository(execution, backend_context):
    request, dependencies, storage = execution
    run_workflow(request, dependencies)
    dependencies.operation.assert_called_once()
    dependencies.projects.set_project_status.assert_called_once_with("project", "done", error=None)
    dependencies.jobs.set_job_status.assert_any_call(1, "done", error=None)
    backend_context.repository.get_project.assert_not_called()
    backend_context.repository.set_project_status.assert_not_called()
    assert storage.close.call_count == 2
    assert (
        dependencies.telemetry.release.call_count == dependencies.telemetry.connect.call_count == 2
    )


def test_runner_publishes_structured_progress_for_the_current_run(execution, monkeypatch):
    request, dependencies, _ = execution
    monkeypatch.setattr(runner, "redis_progress_fn", redis_progress_fn)

    def operation(request, store, progress, bind_client):
        emit_progress(progress, 1, 2, "Annotating", payload={"chapter": 3})
        return "done"

    run_workflow(request, replace(dependencies, operation=operation))

    published = dependencies.telemetry.connect.return_value.publish.call_args_list
    progress_events = []
    for call in published:
        event = json.loads(call.args[1])
        if event["kind"] == request.kind:
            assert call.args[0] == "project:project"
            progress_events.append(event)
    start, tick, finish = progress_events
    assert start["payload"] is finish["payload"] is None
    assert (tick["project_id"], tick["run_id"], tick["kind"]) == ("project", "run", "translation")
    assert (tick["done"], tick["total"], tick["label"]) == (1, 2, "Annotating")
    assert tick["payload"] == {"chapter": 3}


def test_completed_workflow_is_counted(execution):
    from wenyi_backend import metrics

    request, dependencies, _ = execution
    run_workflow(request, dependencies)
    text = metrics.render(dependencies.metrics.snapshot(), depth=0)
    assert 'wenyi_tasks_total{code="none",status="done"} 1' in text


def test_worker_observes_real_retries_after_ledger_rebinding(execution, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from wenyi_core.llm.configuration import LLMConfig
    from wenyi_core.llm.providers.fake import FakeProvider
    from wenyi_core.llm.retrying import EmptyResponseError
    from wenyi_core.llm.router import RoutedLLMClient

    request, dependencies, storage = execution
    client = RoutedLLMClient(
        LLMConfig.model_validate(
            {
                "preset": "fake",
                "providers": {"default": {"kind": "fake", "max_retries": 1}},
            }
        )
    )
    attempts = 0

    def respond(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise EmptyResponseError("empty")
        return "translated"

    monkeypatch.setattr(FakeProvider, "_request", respond)
    monkeypatch.setattr(client.limits, "wait_for_retry", lambda seconds: None)

    def operation(request, store, progress, bind_client):
        bind_client(client)
        # Runtime binds the durable ledger again; independent observers must survive.
        client.set_event_sink(store.log_event)
        with client.capture_events(lambda *args, **kwargs: None):
            with ThreadPoolExecutor(max_workers=1) as pool:
                assert pool.submit(client.complete, [], operation="translation.body").result() == (
                    "translated"
                )
        return "done"

    run_workflow(request, replace(dependencies, operation=operation))
    assert dependencies.metrics.snapshot()["tasks"] == {("done", "none"): 1}
    assert dependencies.metrics.snapshot()["retries"] == {("fake", "empty_response"): 1}
    assert client._event_listeners == {}
    assert any(call.args[0] == "llm_retry_wait" for call in storage.log_event.call_args_list)


@pytest.mark.parametrize(
    "change",
    [
        {"project_id": "other-project"},
        {"kind": "review"},
        {"status": "done"},
        {"id": 2},
    ],
)
def test_invalid_or_duplicate_delivery_never_runs_or_claims_work(execution, change):
    request, dependencies, storage = execution
    original = dependencies.jobs.get_job_by_arq_id.return_value
    dependencies.jobs.get_job_by_arq_id.return_value = {**original, **change}
    runner.execute(request, dependencies)
    dependencies.operation.assert_not_called()
    dependencies.projects.set_project_status.assert_not_called()
    dependencies.jobs.set_job_status.assert_not_called()
    storage.close.assert_called_once()
    dependencies.telemetry.release.assert_called_once()
    assert dependencies.metrics.snapshot()["tasks"] == {}


@pytest.mark.parametrize("failure", ["connect", "thread_start", "release"])
def test_startup_and_cleanup_failures_always_close_storage(execution, monkeypatch, failure):
    request, dependencies, storage = execution
    error = RuntimeError(f"{failure} failed")
    if failure == "thread_start":
        monkeypatch.setattr(runner.threading.Thread, "start", Mock(side_effect=error))
    else:
        getattr(dependencies.telemetry, failure).side_effect = error
    with pytest.raises(RuntimeError, match=f"{failure} failed"):
        runner.execute(request, dependencies)
    storage.close.assert_called_once()
    if failure == "connect":
        dependencies.telemetry.release.assert_not_called()
    else:
        dependencies.telemetry.release.assert_called_once()


def test_operation_failure_drains_resources_and_records_its_own_run(execution):
    request, dependencies, storage = execution
    dependencies.operation.side_effect = ValueError("Domain failed")
    with pytest.raises(ValueError, match="Domain failed"):
        run_workflow(request, dependencies)
    dependencies.projects.set_project_status.assert_called_once_with(
        "project", "error", error="Domain failed"
    )
    dependencies.jobs.set_job_status.assert_any_call(1, "error", error="Domain failed")
    assert storage.close.call_count == 2
    assert dependencies.telemetry.release.call_count == 2
    assert dependencies.metrics.snapshot()["tasks"] == {("error", "pipeline_failed"): 1}


def test_failure_keeps_its_traceback_in_server_logs_only(execution, caplog):
    """Public event payloads retain the classification, not the internal stack."""
    request, dependencies, storage = execution
    dependencies.operation.side_effect = ValueError("Domain failed")
    with pytest.raises(ValueError, match="Domain failed"):
        run_workflow(request, dependencies)
    event = next(
        call.args[0]
        for call in storage.log_event.call_args_list
        if call.args and call.args[0] == "pipeline_error"
    )
    fields = next(
        call.kwargs
        for call in storage.log_event.call_args_list
        if call.args and call.args[0] == "pipeline_error"
    )
    assert event == "pipeline_error"
    assert fields["error_type"] == "ValueError"
    assert "traceback" not in fields
    assert any(record.exc_info for record in caplog.records if record.name == runner.__name__)


def test_failure_publishes_the_code_to_live_subscribers(execution, monkeypatch):
    """T-14: a live client stops waiting and learns the code, not just the text."""
    request, dependencies, storage = execution
    published = []
    monkeypatch.setattr(
        runner, "publish_error", lambda _cache, pid, **kwargs: published.append((pid, kwargs))
    )
    dependencies.operation.side_effect = ValueError("Domain failed")
    with pytest.raises(ValueError):
        run_workflow(request, dependencies)
    assert published, "a failed run must publish an error event"
    project_id, payload = published[0]
    assert project_id == "project"
    assert payload["code"] == "pipeline_failed"
    assert payload["message"] == "Domain failed"
    assert payload["run_id"] == "run"


def test_a_wenyi_error_publishes_its_own_code(execution, monkeypatch):
    """The code a client sees matches the failure it came from."""
    from wenyi_core.errors import WenyiError

    request, dependencies, storage = execution
    published = []
    monkeypatch.setattr(
        runner, "publish_error", lambda _cache, pid, **kwargs: published.append((pid, kwargs))
    )
    dependencies.operation.side_effect = WenyiError("no project", code="project_not_found")
    with pytest.raises(WenyiError):
        run_workflow(request, dependencies)
    assert published[0][1]["code"] == "project_not_found"


@pytest.fixture
def export_execution():
    locked = []
    sequence = []

    @contextmanager
    def export_lock(export_id):
        assert export_id == 4
        locked.append(True)
        try:
            yield
        finally:
            locked.pop()
            sequence.append("unlock")

    def record(event):
        assert locked, f"{event} escaped the export lock"
        sequence.append(event)

    storage = Mock(export_lock=export_lock)
    jobs = Mock()
    jobs.get_job_by_arq_id.return_value = {
        "id": 1,
        "status": "queued",
        "kind": "export",
        "project_id": "project",
        "params": {"export_id": 4},
    }
    jobs.set_job_status.side_effect = lambda identity, status, **kwargs: (
        record(f"job:{status}") or True
    )

    def render(request):
        record("render")
        return "complete-output"

    def publish(pid, export_id, output, *, job_id=None):
        assert job_id == 1
        record("publish")

    dependencies = exporting.ExportDependencies(
        jobs=jobs,
        storage_for=lambda _: storage,
        render=render,
        publish=publish,
        set_status=lambda export_id, status, error=None: record(f"export:{status}"),
    )
    return ExportRequest("project", 4, "run"), dependencies, storage, sequence


def test_export_retains_one_lock_through_publication_and_terminal_status(export_execution):
    request, dependencies, storage, sequence = export_execution
    assert exporting.execute(request, dependencies) == 4
    assert sequence == ["job:running", "export:running", "render", "publish", "unlock"]
    storage.close.assert_called_once()


def test_duplicate_export_never_renders_or_publishes(export_execution):
    request, dependencies, storage, sequence = export_execution
    dependencies.jobs.get_job_by_arq_id.return_value["status"] = "done"
    assert exporting.execute(request, dependencies) == 4
    assert sequence == ["unlock"]
    storage.close.assert_called_once()


@pytest.mark.parametrize("stage", ["render", "publish"])
def test_export_failure_keeps_error_writes_under_the_lock(export_execution, stage):
    request, dependencies, storage, sequence = export_execution

    def fail(*args, **kwargs):
        raise OSError("Export failed")

    dependencies = replace(dependencies, **{stage: fail})
    with pytest.raises(OSError, match="Export failed"):
        exporting.execute(request, dependencies)
    assert sequence[-3:] == ["job:error", "export:error", "unlock"]
    storage.close.assert_called_once()


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("provider timed out"),
        ConnectionError("connection reset"),
    ],
)
def test_resumable_provider_outage_pauses_and_schedules_auto_resume(execution, error):
    from wenyi_backend.execution.outage import AUTO_RESUME_ATTEMPT_PARAM

    request, dependencies, storage = execution
    scheduled = []
    deps = replace(
        dependencies,
        schedule_resume=lambda req, attempt, delay, retry_at: scheduled.append(
            (req, attempt, delay, retry_at)
        ),
    )
    deps.operation.side_effect = error

    # First outage pauses cleanly with a 60s delay.
    run_workflow(request, deps)

    deps.projects.set_project_status.assert_called_once_with("project", "paused", error=str(error))
    deps.jobs.set_job_status.assert_any_call(1, "paused", error=str(error))
    assert deps.metrics.snapshot()["tasks"] == {("paused", "pipeline_failed"): 1}
    assert any(
        call.args and call.args[0] == "task_paused_outage"
        for call in storage.log_event.call_args_list
    )
    assert len(scheduled) == 1
    assert scheduled[0][1] == 1
    assert scheduled[0][2] == 60

    # Subsequent auto-resume attempts back off through 120, 240 and 480 seconds.
    expected_delays = [120, 240, 480]
    for index, expected in enumerate(expected_delays, start=1):
        scheduled.clear()
        retry_request = ExecutionRequest(
            "translation", "project", "run", {AUTO_RESUME_ATTEMPT_PARAM: index}
        )
        run_workflow(retry_request, deps)
        assert scheduled[0][1] == index + 1
        assert scheduled[0][2] == expected
    assert sum([60, *expected_delays]) == 15 * 60


def test_outage_stops_auto_resuming_after_about_fifteen_minutes_without_progress(execution):
    from wenyi_backend.execution.outage import (
        AUTO_RESUME_ATTEMPT_PARAM,
        MAX_AUTO_RESUME_ATTEMPTS,
        ProviderOutageExhausted,
    )

    _request, dependencies, storage = execution
    scheduled = []
    deps = replace(dependencies, schedule_resume=lambda *args: scheduled.append(args))
    deps.operation.side_effect = TimeoutError("provider timed out")
    request = ExecutionRequest(
        "translation", "project", "run", {AUTO_RESUME_ATTEMPT_PARAM: MAX_AUTO_RESUME_ATTEMPTS}
    )

    with pytest.raises(ProviderOutageExhausted, match="about 15 minutes") as raised:
        run_workflow(request, deps)

    assert isinstance(raised.value.__cause__, TimeoutError)
    assert scheduled == []
    deps.projects.set_project_status.assert_called_once_with(
        "project", "error", error=str(raised.value)
    )
    assert deps.metrics.snapshot()["tasks"] == {("error", "provider_unavailable"): 1}
    assert not any(
        call.args and call.args[0] == "task_paused_outage"
        for call in storage.log_event.call_args_list
    )


class _EventClient:
    """Minimal LLM client stand-in that forwards events to the runner's observers."""

    def __init__(self) -> None:
        self.observers = []

    def set_event_sink(self, _sink) -> None:
        pass

    def cancel(self) -> None:
        pass

    def observe_events(self, observer):
        self.observers.append(observer)
        return nullcontext()

    def emit(self, event: str) -> None:
        for observer in self.observers:
            observer(event)


def test_outage_after_a_model_response_restarts_the_retry_streak(execution):
    from wenyi_backend.execution.outage import AUTO_RESUME_ATTEMPT_PARAM, MAX_AUTO_RESUME_ATTEMPTS

    _request, dependencies, _storage = execution
    scheduled = []
    deps = replace(
        dependencies,
        schedule_resume=lambda req, attempt, delay, retry_at: scheduled.append((attempt, delay)),
    )

    def answer_once_then_fail(_request, _storage, _progress, bind):
        client = _EventClient()
        bind(client)
        client.emit("llm_request_scheduled")
        client.emit("llm_request_completed")
        raise TimeoutError("provider timed out")

    deps.operation.side_effect = answer_once_then_fail
    request = ExecutionRequest(
        "translation", "project", "run", {AUTO_RESUME_ATTEMPT_PARAM: MAX_AUTO_RESUME_ATTEMPTS}
    )

    run_workflow(request, deps)

    assert scheduled == [(1, 60)]


def test_rising_progress_without_a_model_response_keeps_the_retry_streak(execution):
    from wenyi_backend.execution.outage import AUTO_RESUME_ATTEMPT_PARAM

    _request, dependencies, _storage = execution
    scheduled = []
    deps = replace(
        dependencies,
        schedule_resume=lambda req, attempt, delay, retry_at: scheduled.append((attempt, delay)),
    )

    def reload_saved_review_then_fail(_request, _storage, progress, bind):
        client = _EventClient()
        bind(client)
        # Resumed Review reports rising chapter counts while it only reloads saved state.
        for position in range(4):
            progress(position, 3, "progress.stage.reviewChapters")
        client.emit("llm_request_scheduled")
        client.emit("llm_request_failed")
        raise TimeoutError("provider timed out")

    deps.operation.side_effect = reload_saved_review_then_fail
    request = ExecutionRequest("translation", "project", "run", {AUTO_RESUME_ATTEMPT_PARAM: 2})

    run_workflow(request, deps)

    assert scheduled == [(3, 240)]


@pytest.mark.parametrize("status_code", [400, 401, 402, 403, 404])
def test_terminal_client_and_config_errors_stay_error_not_paused(execution, status_code):
    from wenyi_core.llm.retrying import ProviderRequestError

    request, dependencies, _storage = execution
    scheduled = []
    deps = replace(
        dependencies,
        schedule_resume=lambda *args: scheduled.append(args),
    )
    error = ProviderRequestError(
        provider="openai-compatible",
        operation="translation.body",
        error_type=f"http_{status_code}",
        status_code=status_code,
        reason=None,
        resumable=status_code == 402,
    )
    deps.operation.side_effect = error

    with pytest.raises(ProviderRequestError):
        run_workflow(request, deps)

    deps.projects.set_project_status.assert_called_once_with("project", "error", error=str(error))
    deps.jobs.set_job_status.assert_any_call(1, "error", error=str(error))
    assert scheduled == []


def test_resumable_http_500_provider_error_pauses_and_schedules_resume(execution):
    from wenyi_core.llm.retrying import ProviderRequestError

    request, dependencies, _storage = execution
    scheduled = []
    deps = replace(
        dependencies,
        schedule_resume=lambda *args: scheduled.append(args),
    )
    error = ProviderRequestError(
        provider="openai-compatible",
        operation="translation.body",
        error_type="http_500",
        status_code=500,
        reason="http_500",
        resumable=True,
    )
    deps.operation.side_effect = error

    run_workflow(request, deps)

    deps.projects.set_project_status.assert_called_once_with("project", "paused", error=str(error))
    deps.jobs.set_job_status.assert_any_call(1, "paused", error=str(error))
    assert len(scheduled) == 1
    assert scheduled[0][1] == 1
    assert scheduled[0][2] == 60


def test_run_auto_resume_resumes_paused_job_and_respects_cancellation(backend_context, monkeypatch):
    from wenyi_backend import job_service
    from wenyi_backend.execution.outage import AUTO_RESUME_ATTEMPT_PARAM
    from wenyi_backend.workers import tasks
    from wenyi_backend.workers.signing import SIGNATURE_FIELD, sign_task

    cache = {"project:p1:auto_resume": "run-1"}
    backend_context.telemetry.connect = lambda: SimpleNamespace(
        get=lambda key: cache.get(key),
        set=lambda key, value, **kwargs: cache.__setitem__(key, value),
    )
    backend_context.repository.get_project.return_value = {
        "id": "p1",
        "status": "paused",
        "source_path": "/tmp/book.txt",
        "source_lang": "zh",
        "target_lang": "en",
    }
    backend_context.repository.latest_resumable_job.return_value = {
        "id": 7,
        "project_id": "p1",
        "kind": "translation",
        "run_id": "run-1",
        "status": "paused",
        "params": {"autofix": False},
    }
    started = []

    async def fake_start_job(pid, kind, **kwargs):
        started.append((pid, kind, kwargs["params"]))
        return {"job_id": "run-2", "project_id": pid, "kind": kind}

    monkeypatch.setattr(job_service, "start_job", fake_start_job)
    payload = {"project_id": "p1", "expected_run_id": "run-1", "attempt": 2}
    signed_payload = {
        **payload,
        SIGNATURE_FIELD: sign_task("run_auto_resume", payload, backend_context.api_token),
    }

    assert asyncio.run(tasks.run_auto_resume({"backend": backend_context}, **signed_payload))
    assert started == [("p1", "translation", {"autofix": False, AUTO_RESUME_ATTEMPT_PARAM: 2})]

    # Cleared marker (manual resume / pause / delete) cancels the scheduled retry.
    started.clear()
    cache["project:p1:auto_resume"] = ""
    assert not asyncio.run(tasks.run_auto_resume({"backend": backend_context}, **signed_payload))
    assert started == []

    # An unsigned queue payload is rejected before it can restart a paused job.
    cache["project:p1:auto_resume"] = "run-1"
    with pytest.raises(ValueError, match="unsigned or tampered run_auto_resume"):
        asyncio.run(tasks.run_auto_resume({"backend": backend_context}, **payload))
    assert started == []


def test_native_panic_lands_failed_terminal_state(execution):
    """pyo3 wraps Rust panics in BaseException subclasses; the run must not stay running."""
    request, dependencies, storage = execution

    class NativePanic(BaseException):
        pass

    dependencies.operation.side_effect = NativePanic("native engine panicked")
    with pytest.raises(NativePanic, match="native engine panicked"):
        run_workflow(request, dependencies)
    dependencies.projects.set_project_status.assert_called_once_with(
        "project", "error", error="native engine panicked"
    )
    dependencies.jobs.set_job_status.assert_any_call(1, "error", error="native engine panicked")
    assert dependencies.metrics.snapshot()["tasks"] == {("error", "pipeline_failed"): 1}
    fields = next(
        call.kwargs
        for call in storage.log_event.call_args_list
        if call.args and call.args[0] == "pipeline_error"
    )
    assert fields["error_type"] == "NativePanic"
    assert storage.close.call_count == 2
    assert dependencies.telemetry.release.call_count == 2


def test_export_native_panic_keeps_error_writes_under_the_lock(export_execution):
    """An export panic still writes its error terminal states inside the lock."""
    request, dependencies, storage, sequence = export_execution

    class NativePanic(BaseException):
        pass

    def fail(*args):
        raise NativePanic("native render panicked")

    dependencies = replace(dependencies, render=fail)
    with pytest.raises(NativePanic, match="native render panicked"):
        exporting.execute(request, dependencies)
    assert sequence[-3:] == ["job:error", "export:error", "unlock"]
    storage.close.assert_called_once()


def test_cancelled_workflow_drains_checkpoint_and_reports_drained(execution):
    request, dependencies, _ = execution
    release = asyncio.Event()
    entered = asyncio.Event()

    def operation(request, store, progress, bind_client):
        return "done"

    async def run_sync(operation):
        outcome = operation()
        entered.set()
        await release.wait()
        return outcome

    async def scenario():
        task = asyncio.create_task(
            runner.run(request, replace(dependencies, operation=operation, run_sync=run_sync))
        )
        await asyncio.wait_for(entered.wait(), 5.0)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5.0)

    asyncio.run(scenario())
    assert dependencies.metrics.snapshot()["task_cancel_drains"] == {("drained", "lt_1s"): 1}


def test_slow_cancel_drains_a_real_locked_checkpoint(execution, monkeypatch, tmp_path):
    request, dependencies, _ = execution
    monkeypatch.setattr(runner, "CANCEL_DRAIN_WARNING_SECONDS", 0.02)
    release = threading.Event()
    finish_cleanup = threading.Event()
    run_dir = str(tmp_path / "project")

    async def scenario():
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        slow = asyncio.Event()
        cleaning = asyncio.Event()
        terminal = Mock(wraps=runner.record_terminal_status)
        monkeypatch.setattr(runner, "record_terminal_status", terminal)
        monkeypatch.setattr(runner.log, "warning", lambda *args, **kw: slow.set())

        def operation(request, store, progress, bind_client):
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(10)
            store.write_artifact("checkpoint.json", {"saved": True})
            progress(1, 1, "checkpoint saved")
            pytest.fail("The operation did not observe cancellation")

        def release_telemetry(cache):
            loop.call_soon_threadsafe(cleaning.set)
            assert finish_cleanup.wait(10)

        dependencies.telemetry.release.side_effect = release_telemetry
        task = asyncio.create_task(
            runner.run(
                request,
                replace(
                    dependencies,
                    operation=operation,
                    storage_for=lambda _: SqliteStorage(run_dir),
                ),
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel()
            await asyncio.wait_for(slow.wait(), 5)
            with closing(SqliteStorage(run_dir)) as probe:
                with pytest.raises(BlockingIOError), probe.lock(blocking=False):
                    pytest.fail("The checkpoint writer must retain the run lock")
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            terminal.assert_not_called()
            dependencies.projects.set_project_status.assert_not_called()
            assert dependencies.metrics.snapshot()["task_cancel_drains"] == {}

            release.set()
            await asyncio.wait_for(cleaning.wait(), 5)
            assert not task.done(), "The writer is still cleaning up"
            dependencies.projects.set_project_status.assert_not_called()
        finally:
            release.set()
            finish_cleanup.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)

        with closing(SqliteStorage(run_dir)) as probe, probe.lock(blocking=False):
            assert probe.read_artifact("checkpoint.json") == {"saved": True}

    asyncio.run(scenario())
    counts = dependencies.metrics.snapshot()["task_cancel_drains"]
    assert [(outcome, count) for (outcome, _), count in counts.items()] == [("drained", 1)]
    dependencies.jobs.set_job_status.assert_any_call(1, "paused", error=None)


@pytest.mark.parametrize("paused", [False, True])
def test_late_worker_outcome_cannot_overwrite_a_newer_run(execution, tmp_path, paused):
    request, dependencies, _ = execution
    run_dir = str(tmp_path / "project")
    old = dependencies.jobs.get_job_by_arq_id.return_value
    new = {**old, "id": 2, "run_id": "new-run"}

    def operation(request, store, progress, bind_client):
        store.write_artifact("checkpoint.json", {"run": "run"})
        if paused:
            raise runner.PauseRequested(request.project_id)
        return "done"

    async def supersede_at_writer_exit(write):
        outcome = await asyncio.to_thread(write)
        with closing(SqliteStorage(run_dir)) as probe, probe.lock(blocking=False):
            assert probe.read_artifact("checkpoint.json") == {"run": "run"}
            dependencies.jobs.list_jobs.return_value = [new, old]
            probe.write_artifact("checkpoint.json", {"run": "new-run"})
        return outcome

    run_workflow(
        request,
        replace(
            dependencies,
            storage_for=lambda _: SqliteStorage(run_dir),
            operation=operation,
            run_sync=supersede_at_writer_exit,
        ),
    )
    dependencies.projects.set_project_status.assert_not_called()
    assert all(call.args[0] == 1 for call in dependencies.jobs.set_job_status.call_args_list)
    dependencies.telemetry.connect.return_value.publish.assert_not_called()
    with closing(SqliteStorage(run_dir)) as probe:
        assert probe.read_artifact("checkpoint.json") == {"run": "new-run"}
