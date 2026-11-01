"""Explicit execution ports preserve cleanup, identity and publication lock ownership."""

import asyncio
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from wenyi_backend.execution import exporting, runner
from wenyi_backend.execution.contracts import ExecutionDependencies, ExecutionRequest, ExportRequest


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


def test_runner_uses_supplied_ports_not_ambient_repository(execution, backend_context):
    request, dependencies, storage = execution
    runner.execute(request, dependencies)
    dependencies.operation.assert_called_once()
    dependencies.projects.set_project_status.assert_called_once_with("project", "done")
    dependencies.jobs.set_job_status.assert_any_call(1, "done")
    backend_context.repository.get_project.assert_not_called()
    backend_context.repository.set_project_status.assert_not_called()
    storage.close.assert_called_once()
    dependencies.telemetry.release.assert_called_once_with(
        dependencies.telemetry.connect.return_value
    )


def test_completed_workflow_is_counted(execution):
    from wenyi_backend import metrics

    request, dependencies, _ = execution
    runner.execute(request, dependencies)
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

    runner.execute(request, replace(dependencies, operation=operation))
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
        runner.execute(request, dependencies)
    dependencies.projects.set_project_status.assert_called_once_with(
        "project", "error", error="Domain failed"
    )
    dependencies.jobs.set_job_status.assert_any_call(1, "error", error="Domain failed")
    assert storage.close.call_count >= 1
    dependencies.telemetry.release.assert_called_once()
    assert dependencies.metrics.snapshot()["tasks"] == {("error", "pipeline_failed"): 1}


def test_failure_keeps_its_traceback_in_server_logs_only(execution, caplog):
    """Public event payloads retain the classification, not the internal stack."""
    request, dependencies, storage = execution
    dependencies.operation.side_effect = ValueError("Domain failed")
    with pytest.raises(ValueError, match="Domain failed"):
        runner.execute(request, dependencies)
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
        runner.execute(request, dependencies)
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
        runner.execute(request, dependencies)
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
    jobs.set_job_status.side_effect = lambda identity, status, **kwargs: record(f"job:{status}")

    def render(request):
        record("render")
        return "complete-output"

    dependencies = exporting.ExportDependencies(
        jobs=jobs,
        storage_for=lambda _: storage,
        render=render,
        publish=lambda pid, eid, output: record("publish"),
        set_status=lambda identity, status, **kwargs: record(f"export:{status}"),
    )
    return ExportRequest("project", 4, "run"), dependencies, storage, sequence


def test_export_retains_one_lock_through_publication_and_terminal_status(export_execution):
    request, dependencies, storage, sequence = export_execution
    assert exporting.execute(request, dependencies) == 4
    assert sequence == ["job:running", "export:running", "render", "publish", "job:done", "unlock"]
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

    def fail(*args):
        raise OSError("Export failed")

    dependencies = replace(dependencies, **{stage: fail})
    with pytest.raises(OSError, match="Export failed"):
        exporting.execute(request, dependencies)
    assert sequence[-3:] == ["job:error", "export:error", "unlock"]
    storage.close.assert_called_once()
