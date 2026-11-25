"""Update admission uses temporary catalogs and mock tasks, never real providers."""

import asyncio
import hashlib
import json
import shutil
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from wenyi_desktop import paths
from wenyi_desktop.local_backend import LocalBackend
from wenyi_desktop.local_runtime import LocalRuntime
from wenyi_desktop.main import DesktopServices, create_app, create_context
from wenyi_desktop.settings import DesktopSettings
from wenyi_desktop.updates import UpdateAdmissionMiddleware, UpdateGuard


@pytest.fixture
def context(tmp_path):
    context = create_context(tmp_path / "updates", api_token="test-token")
    backend = context.repository
    assert isinstance(backend, LocalBackend)
    yield context
    backend.close()


def test_authenticated_private_endpoints_and_isolation(context, tmp_path):
    app = create_app(context=context)
    context.telemetry.runtime = SimpleNamespace(accepting=True, idle=True)
    other = create_context(tmp_path / "other", api_token="other-token")
    other_services = other.telemetry
    assert isinstance(other_services, DesktopServices)
    other_app = create_app(context=other)
    other_services.runtime = cast(LocalRuntime, SimpleNamespace(accepting=True, idle=True))
    try:
        # Lifespan intentionally not entered: no real workers are needed.
        client = TestClient(app)
        peer = TestClient(other_app)
        headers = {"Authorization": "Bearer test-token"}
        assert client.post("/desktop/updates/prepare").status_code == 401
        assert client.post("/desktop/updates/cancel").status_code == 401
        assert client.post("/desktop/updates/prepare", headers=headers).status_code == 204
        assert client.post("/desktop/updates/prepare", headers=headers).status_code == 204
        assert client.put("/config", headers=headers).status_code == 409
        assert client.put("/desktop/credentials/test", headers=headers).status_code == 409
        assert client.post("/projects/upload", headers=headers).status_code == 409
        assert client.put("/config").status_code == 401
        assert not other_app.state.update_guard.closed
        assert (
            peer.post(
                "/desktop/updates/prepare", headers={"Authorization": "Bearer other-token"}
            ).status_code
            == 204
        )
        assert client.post("/desktop/updates/cancel", headers=headers).status_code == 204
        assert client.post("/desktop/updates/cancel", headers=headers).status_code == 204
        assert client.put("/config", headers=headers).status_code != 409
        assert other_app.state.update_guard.closed
        assert "/desktop/updates/prepare" not in app.openapi()["paths"]
        context.telemetry.runtime = None
        assert client.post("/desktop/updates/prepare", headers=headers).status_code == 503
        assert client.post("/desktop/updates/cancel", headers=headers).status_code == 503
    finally:
        other_backend = other.repository
        assert isinstance(other_backend, LocalBackend)
        other_backend.close()


def test_backup_plan_requires_frozen_authenticated_admission(context, monkeypatch, tmp_path):
    app = create_app(context=context)
    context.telemetry.runtime = SimpleNamespace(accepting=True, idle=True)
    client = TestClient(app)
    headers = {"Authorization": "Bearer test-token"}
    project = tmp_path / "chosen" / "0123456789abcdef-Book"
    project.mkdir(parents=True)
    # The catalog/path adapter owns safe enumeration; the guard must call it only
    # after preparation has excluded new writes.
    calls = []

    def directories():
        calls.append(app.state.update_guard.closed)
        return (project,)

    monkeypatch.setattr(context.repository, "project_directories", directories, raising=False)
    assert client.post("/desktop/updates/backup-plan").status_code == 401
    assert client.post("/desktop/updates/backup-plan", headers=headers).status_code == 409
    assert not calls
    assert client.post("/desktop/updates/prepare", headers=headers).status_code == 204
    response = client.post("/desktop/updates/backup-plan", headers=headers)
    assert response.status_code == 200
    assert calls == [True]
    roots = response.json()["sources"]
    assert {"path": str(project), "name": "projects/0"} in roots
    assert {"path": str(context.repository.database), "name": "catalog.sqlite3"} in roots
    assert not any(row["path"] == str(project.parent) for row in roots)
    assert client.put("/config", headers=headers).status_code == 409
    assert client.post("/desktop/updates/cancel", headers=headers).status_code == 204
    assert client.post("/desktop/updates/backup-plan", headers=headers).status_code == 409


def test_explicit_root_overrides_xdg_for_catalog_credentials_and_backup_plan(
    tmp_path, monkeypatch, private_profile
):
    if paths.sys.platform in {"win32", "darwin"}:
        pytest.skip("XDG_DATA_HOME applies only to XDG platforms")
    data_home = tmp_path / "separate-data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    root = tmp_path / "workspace"
    context = create_context(root, api_token="test-token")
    backend = context.repository
    assert isinstance(backend, LocalBackend)
    settings = context.settings_store
    assert isinstance(settings, DesktopSettings)
    services = context.telemetry
    assert isinstance(services, DesktopServices)
    try:
        assert backend.database == root / "catalog.sqlite3"
        assert settings.credentials.database == backend.database
        services.runtime = cast(LocalRuntime, SimpleNamespace(accepting=True, idle=True))
        client = TestClient(create_app(context=context))
        headers = {"Authorization": "Bearer test-token"}
        assert client.post("/desktop/updates/prepare", headers=headers).status_code == 204
        response = client.post("/desktop/updates/backup-plan", headers=headers)
        assert response.status_code == 200
        assert response.json()["sources"] == [
            {"path": str(backend.database), "name": "catalog.sqlite3"},
        ]
        assert not data_home.exists()
        assert not (private_profile / "Wenyi").exists()
    finally:
        backend.close()


def test_missing_authoritative_project_refuses_backup_plan(context, monkeypatch):
    app = create_app(context=context)
    context.telemetry.runtime = SimpleNamespace(accepting=True, idle=True)
    client = TestClient(app)
    headers = {"Authorization": "Bearer test-token"}

    def missing():
        raise HTTPException(409, "A registered project directory is unavailable")

    monkeypatch.setattr(context.repository, "project_directories", missing, raising=False)
    assert client.post("/desktop/updates/prepare", headers=headers).status_code == 204
    response = client.post("/desktop/updates/backup-plan", headers=headers)
    assert response.status_code == 409
    assert "registered project" in response.json()["detail"]
    assert app.state.update_guard.closed


def test_backup_plan_discovers_all_live_layouts_without_copying_parent_folders(context, tmp_path):
    backend = context.repository
    legacy = backend.create_project("Legacy", "en", "zh", {})
    backend.mark_project(legacy)
    backend.library_root = tmp_path / "Default library"
    current = backend.create_project("Default", "en", "zh", {})
    backend.mark_project(current)
    chosen = backend.create_project(
        "Chosen", "en", "zh", {}, library_root=tmp_path / "Chosen library"
    )
    backend.mark_project(chosen)
    app = create_app(context=context)
    context.telemetry.runtime = SimpleNamespace(accepting=True, idle=True)
    client = TestClient(app)
    headers = {"Authorization": "Bearer test-token"}
    assert client.post("/desktop/updates/prepare", headers=headers).status_code == 204
    response = client.post("/desktop/updates/backup-plan", headers=headers)
    assert response.status_code == 200
    sources = {row["path"] for row in response.json()["sources"]}
    assert sources == {
        str(backend.database),
        *(str(backend.project_dir(pid)) for pid in (legacy, current, chosen)),
    }
    # Removal of a drive/folder is not a successful backup of the remaining data.
    backend.project_dir(chosen).rename(tmp_path / "disconnected")
    response = client.post("/desktop/updates/backup-plan", headers=headers)
    assert response.status_code == 409
    assert response.json()["code"] == "project_location_unavailable"
    assert chosen in response.json()["message"]


@pytest.mark.parametrize("kind", ["parse", "translation", "review", "export"])
@pytest.mark.parametrize("status", ["queued", "running"])
def test_all_project_jobs_block_prepare(context, kind, status):
    backend = context.repository
    for name in ("one", "two"):
        pid = backend.create_project(name, "en", "zh", {})
    job = backend.create_job(pid, kind, "mock-task", run_id="mock-run")
    backend.set_job_status(job, status)
    runtime = SimpleNamespace(accepting=True, idle=True)
    context.telemetry.runtime = runtime
    guard = UpdateGuard(context.telemetry)
    with pytest.raises(HTTPException) as error:
        asyncio.run(guard.prepare())
    assert error.value.status_code == 409
    assert not guard.closed
    assert runtime.accepting


@pytest.mark.parametrize("owned", ["_submitted", "_active", "_cancelling", "_workflows"])
def test_actual_runtime_ownership_blocks_even_terminal_catalog(context, owned):
    async def run():
        runtime = LocalRuntime(context.repository, context)
        runtime.accepting = True
        context.telemetry.runtime = runtime
        pid = context.repository.create_project("terminal", "en", "zh", {})
        job = context.repository.create_job(pid, "export", "mock", run_id="mock")
        context.repository.set_job_status(job, "done")
        task = asyncio.create_task(asyncio.Event().wait())
        if owned == "_active":
            runtime._active["mock"] = task
        else:
            getattr(runtime, owned).add("mock" if owned == "_submitted" else task)
        guard = UpdateGuard(context.telemetry)
        try:
            with pytest.raises(HTTPException) as error:
                await guard.prepare()
            assert error.value.status_code == 409
            assert runtime.accepting and not task.done() and not guard.closed
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_asgi_inflight_mutation_and_future_admission(context):
    async def run():
        context.telemetry.runtime = SimpleNamespace(accepting=True, idle=True)
        guard = UpdateGuard(context.telemetry)
        entered, release = asyncio.Event(), asyncio.Event()

        async def app(scope, receive, send):
            entered.set()
            await release.wait()

        middleware = UpdateAdmissionMiddleware(app, guard=guard, token=None)
        scope = {"type": "http", "method": "PUT", "path": "/config", "headers": []}
        request = asyncio.create_task(middleware(scope, None, None))
        await entered.wait()
        with pytest.raises(HTTPException):
            await guard.prepare()
        assert not guard.closed and guard.inflight == 1
        release.set()
        await request
        assert guard.inflight == 0
        await guard.prepare()
        messages = []

        async def send(message):
            messages.append(message)

        await middleware(scope, None, send)
        assert messages[0]["status"] == 409
        await guard.cancel()
        await middleware(scope, None, None)
        assert not guard.closed

    asyncio.run(run())


def test_invalid_header_bytes_are_rejected_without_entering_a_mutation(context):
    async def run():
        guard = UpdateGuard(context.telemetry)
        sent = []

        async def app(scope, receive, send):
            pytest.fail("Invalid credentials must not reach the application")

        async def send(message):
            sent.append(message)

        middleware = UpdateAdmissionMiddleware(app, guard=guard, token="test-token")
        scope = {
            "type": "http",
            "method": "PUT",
            "path": "/config",
            "headers": [(b"authorization", b"Bearer \xff")],
        }
        await middleware(scope, None, send)
        assert sent[0]["status"] == 401
        assert guard.inflight == 0

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["error", "cancel", "success"])
def test_check_closes_gate_and_serializes_cancel(context, monkeypatch, failure):
    async def run():
        context.telemetry.runtime = SimpleNamespace(accepting=True, idle=True)
        guard = UpdateGuard(context.telemetry)
        entered, release = threading.Event(), threading.Event()

        def check():
            entered.set()
            release.wait(5)
            if failure == "error":
                raise ValueError("mock catalog error")
            return []

        monkeypatch.setattr(context.repository, "all_jobs", check)
        prepare = asyncio.create_task(guard.prepare())
        await asyncio.to_thread(entered.wait, 5)
        assert guard.closed
        messages = []

        async def send(message):
            messages.append(message)

        async def forbidden(scope, receive, send):
            pytest.fail("A mutation entered during the asynchronous idle check")

        middleware = UpdateAdmissionMiddleware(forbidden, guard=guard, token=None)
        await middleware(
            {"type": "http", "method": "POST", "path": "/projects/upload", "headers": []},
            None,
            send,
        )
        assert messages[0]["status"] == 409
        cancel = asyncio.create_task(guard.cancel())
        await asyncio.sleep(0)
        assert not cancel.done() and guard.closed
        if failure == "cancel":
            prepare.cancel()
        release.set()
        result = await asyncio.gather(prepare, return_exceptions=True)
        if failure == "error":
            assert isinstance(result[0], ValueError)
        elif failure == "cancel":
            assert isinstance(result[0], asyncio.CancelledError)
        await cancel
        assert not guard.closed and context.telemetry.runtime.accepting

    asyncio.run(run())


@pytest.mark.parametrize("cancelled", [False, True])
def test_failed_check_reopens_without_cancel_request(context, monkeypatch, cancelled):
    async def run():
        context.telemetry.runtime = SimpleNamespace(accepting=True, idle=True)
        guard = UpdateGuard(context.telemetry)
        entered, release = threading.Event(), threading.Event()

        def check():
            entered.set()
            release.wait(5)
            raise ValueError("mock failure")

        monkeypatch.setattr(context.repository, "all_jobs", check)
        request = asyncio.create_task(guard.prepare())
        await asyncio.to_thread(entered.wait, 5)
        if cancelled:
            request.cancel()
        release.set()
        result = await asyncio.gather(request, return_exceptions=True)
        assert isinstance(result[0], asyncio.CancelledError if cancelled else ValueError)
        assert not guard.closed

    asyncio.run(run())


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_backup(workspace: Path, version: str, sources: dict[str, Path]) -> Path:
    """Mimic the native snapshot: hashed per-source copies, manifest written last."""
    backup = workspace / "backups" / f"{version}-20261120-000000"
    entries = []
    for name, source in sources.items():
        pairs = (
            [(source, name)]
            if source.is_file()
            else [
                (file, f"{name}/{file.relative_to(source).as_posix()}")
                for file in sorted(source.rglob("*"))
                if file.is_file() and not file.name.endswith(".lock")
            ]
        )
        for file, relative in pairs:
            target = backup / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(file, target)
            entries.append({"path": relative, "size": file.stat().st_size, "sha256": _digest(file)})
    manifest = {
        "schema_version": 2,
        "source_path": "",
        "created_at": "2026-11-20T00:00:00Z",
        "version": version,
        "files": entries,
        "sources": [{"name": name, "path": str(path)} for name, path in sources.items()],
    }
    (backup / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    return backup


def test_unrelated_startup_error_does_not_replace_live_sqlite(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    catalog = workspace / "catalog.sqlite3"
    with sqlite3.connect(catalog) as connection:
        connection.execute("CREATE TABLE example (value TEXT)")
        connection.execute("INSERT INTO example VALUES ('before upgrade')")
    _write_backup(workspace, "1.9.0", {"catalog.sqlite3": catalog})
    with sqlite3.connect(catalog) as connection:
        connection.execute("UPDATE example SET value = 'live data'")

    def unavailable(*args, **kwargs):
        raise RuntimeError("unrelated startup error")

    monkeypatch.setattr(LocalBackend, "__init__", unavailable)
    with pytest.raises(RuntimeError, match="unrelated startup error"):
        create_context(workspace)
    with sqlite3.connect(catalog) as connection:
        assert connection.execute("SELECT value FROM example").fetchone() == ("live data",)
