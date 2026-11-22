"""Update admission uses temporary catalogs and mock tasks, never real providers."""

import asyncio
import hashlib
import json
import logging
import shutil
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from wenyi_desktop import paths
from wenyi_desktop.local_backend import LocalBackend
from wenyi_desktop.local_runtime import LocalRuntime
from wenyi_desktop.main import create_app, create_context
from wenyi_desktop.updates import (
    RETENTION_KEEP_BACKUPS,
    RETENTION_MAX_BYTES,
    PruneOutcome,
    UpdateAdmissionMiddleware,
    UpdateGuard,
    prune_backups,
    restore_latest_backup,
)


@pytest.fixture
def context(tmp_path):
    context = create_context(tmp_path / "updates", api_token="test-token")
    yield context
    context.repository.close()


def test_authenticated_private_endpoints_and_isolation(context, tmp_path):
    app = create_app(context=context)
    context.telemetry.runtime = SimpleNamespace(accepting=True, idle=True)
    other = create_context(tmp_path / "other", api_token="other-token")
    other_app = create_app(context=other)
    other.telemetry.runtime = SimpleNamespace(accepting=True, idle=True)
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
        other.repository.close()


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
    try:
        assert context.repository.database == root / "catalog.sqlite3"
        assert context.settings_store.credentials.database == context.repository.database
        context.telemetry.runtime = SimpleNamespace(accepting=True, idle=True)
        client = TestClient(create_app(context=context))
        headers = {"Authorization": "Bearer test-token"}
        assert client.post("/desktop/updates/prepare", headers=headers).status_code == 204
        response = client.post("/desktop/updates/backup-plan", headers=headers)
        assert response.status_code == 200
        assert response.json()["sources"] == [
            {"path": str(context.repository.database), "name": "catalog.sqlite3"},
        ]
        assert not data_home.exists()
        assert not (private_profile / "Wenyi").exists()
    finally:
        context.repository.close()


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


def _seed_migration_failure(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    workspace = tmp_path / "workspace"
    catalog = workspace / "catalog.sqlite3"
    project = workspace / "projects" / "0"
    project.mkdir(parents=True)
    catalog.write_bytes(b"pre-upgrade catalog")
    (project / "book.epub").write_bytes(b"pre-upgrade project")
    (project / "run.lock").write_bytes(b"runtime lock")
    backup = _write_backup(workspace, "1.4.0", {"catalog.sqlite3": catalog, "projects/0": project})
    # The upgrade half-migrated the live data before the new engine gave up.
    catalog.write_bytes(b"half-migrated catalog")
    (project / "book.epub").write_bytes(b"half-migrated project")
    return workspace, backup, catalog, project


def test_restore_recovers_the_workspace_after_a_failed_migration(tmp_path):
    workspace, backup, catalog, project = _seed_migration_failure(tmp_path)

    outcome = restore_latest_backup(workspace)

    assert outcome.status == "restored"
    assert outcome.files == 2
    assert catalog.read_bytes() == b"pre-upgrade catalog"
    assert (project / "book.epub").read_bytes() == b"pre-upgrade project"
    # Runtime locks are never part of the snapshot and stay untouched.
    assert (project / "run.lock").read_bytes() == b"runtime lock"
    assert (backup / "MANIFEST.restored.json").is_file()
    assert not (backup / "MANIFEST.json").exists()


def test_restore_refuses_a_damaged_backup_without_touching_the_workspace(tmp_path):
    workspace, backup, catalog, project = _seed_migration_failure(tmp_path)
    (backup / "catalog.sqlite3").write_bytes(b"tampered catalog!!!")

    outcome = restore_latest_backup(workspace)

    assert outcome.status == "failed"
    assert "checksum mismatch" in outcome.detail
    assert catalog.read_bytes() == b"half-migrated catalog"
    assert (project / "book.epub").read_bytes() == b"half-migrated project"
    assert (backup / "MANIFEST.json").is_file()
    assert not (backup / "MANIFEST.restored.json").exists()


def test_restore_without_a_backup_reports_absent(tmp_path):
    outcome = restore_latest_backup(tmp_path / "workspace")

    assert outcome.status == "absent"
    assert outcome.files == 0


def test_restore_reports_failure_when_backups_directory_is_unreadable(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original_iterdir = Path.iterdir

    def failing_iterdir(self):
        if self.name == "backups":
            raise PermissionError(13, "backups denied")
        return original_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", failing_iterdir)
    outcome = restore_latest_backup(workspace)
    assert outcome.status == "failed"
    assert outcome.detail is not None and "unreadable" in outcome.detail


def test_restore_never_clobbers_data_after_a_backup_was_consumed(tmp_path):
    workspace, _, catalog, _ = _seed_migration_failure(tmp_path)
    assert restore_latest_backup(workspace).status == "restored"
    catalog.write_bytes(b"new attempt bytes")

    outcome = restore_latest_backup(workspace)

    assert outcome.status == "absent"
    assert catalog.read_bytes() == b"new attempt bytes"


def test_restore_resumes_after_an_interrupted_copy(tmp_path, monkeypatch):
    workspace, backup, catalog, project = _seed_migration_failure(tmp_path)
    original = shutil.copyfile
    attempts = {"count": 0}

    def interrupted(source, target):
        attempts["count"] += 1
        if attempts["count"] == 2:
            raise OSError("simulated interruption")
        return original(source, target)

    monkeypatch.setattr(shutil, "copyfile", interrupted)
    assert restore_latest_backup(workspace).status == "failed"
    # Work completed before the interruption stays; the manifest survives.
    assert catalog.read_bytes() == b"pre-upgrade catalog"
    assert (backup / "MANIFEST.json").is_file()

    monkeypatch.setattr(shutil, "copyfile", original)
    assert restore_latest_backup(workspace).status == "restored"
    assert (project / "book.epub").read_bytes() == b"pre-upgrade project"
    assert (backup / "MANIFEST.restored.json").is_file()


def test_restore_rejects_entries_outside_the_declared_sources(tmp_path):
    workspace, backup, catalog, _ = _seed_migration_failure(tmp_path)
    manifest = json.loads((backup / "MANIFEST.json").read_text(encoding="utf-8"))
    manifest["files"].append({"path": "projects/9/extra.epub", "size": 3, "sha256": "0" * 64})
    (backup / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")

    outcome = restore_latest_backup(workspace)

    assert outcome.status == "failed"
    assert "outside declared sources" in outcome.detail
    assert catalog.read_bytes() == b"half-migrated catalog"


def test_restore_covers_project_directories_outside_the_data_root(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    library = tmp_path / "library" / "0"
    library.mkdir(parents=True)
    (library / "book.epub").write_bytes(b"pre-upgrade project")
    _write_backup(workspace, "1.4.0", {"projects/0": library})
    (library / "book.epub").write_bytes(b"half-migrated project")

    outcome = restore_latest_backup(workspace)

    assert outcome.status == "restored"
    assert (library / "book.epub").read_bytes() == b"pre-upgrade project"


def test_failed_bootstrap_restores_and_tells_the_shell(tmp_path, capsys, monkeypatch):
    workspace, _, catalog, _ = _seed_migration_failure(tmp_path)

    def refused(self, *args, **kwargs):
        raise RuntimeError("This is not a current-format Desktop catalog.")

    monkeypatch.setattr(LocalBackend, "__init__", refused)
    with pytest.raises(RuntimeError):
        create_context(workspace, api_token="test-token")
    output = capsys.readouterr().out
    assert output.startswith("WENYI_FAILED ")
    assert '"reason":"migration"' in output
    assert '"restored":true' in output
    assert catalog.read_bytes() == b"pre-upgrade catalog"


def _seed_backups(workspace: Path, names: list[str], payload: bytes = b"payload") -> None:
    """Create fake upgrade snapshots: one payload file, manifest written last."""
    for name in names:
        backup = workspace / "backups" / name
        backup.mkdir(parents=True, exist_ok=True)
        (backup / "catalog.sqlite3").write_bytes(payload)
        (backup / "MANIFEST.json").write_text("{}", encoding="utf-8")


def test_prune_reports_unreadable_backups_directory(tmp_path, monkeypatch, caplog):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original_iterdir = Path.iterdir

    def failing_iterdir(self):
        if self.name == "backups":
            raise PermissionError(13, "backups denied")
        return original_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", failing_iterdir)
    with caplog.at_level(logging.WARNING, logger="wenyi_desktop.updates"):
        outcome = prune_backups(workspace)
    assert outcome == PruneOutcome()
    assert any("Backups directory scan failed" in message for message in caplog.messages)


def test_prune_keeps_the_newest_and_the_configured_count(tmp_path):
    workspace = tmp_path / "workspace"
    names = [f"1.4.0-2026112{day}-000000" for day in range(1, 6)]
    _seed_backups(workspace, names)
    outcome = prune_backups(workspace, keep=3, delete=shutil.rmtree)
    # Eviction follows the newest-first traversal, so the older of the two
    # surplus snapshots is reported last.
    assert outcome.evicted == (names[1], names[0])
    assert outcome.kept == (names[4], names[3], names[2])
    assert outcome.failed == ()
    assert not (workspace / "backups" / names[0]).exists()
    assert (workspace / "backups" / names[-1]).exists()


def test_prune_enforces_the_size_budget(tmp_path):
    workspace = tmp_path / "workspace"
    old, middle, newest = (
        "1.4.0-20261121-000000",
        "1.4.0-20261122-000000",
        "1.4.0-20261123-000000",
    )
    _seed_backups(workspace, [old], payload=b"o" * 50)
    _seed_backups(workspace, [middle], payload=b"m" * 40)
    _seed_backups(workspace, [newest], payload=b"n" * 30)
    outcome = prune_backups(workspace, keep=10, max_bytes=100, delete=lambda path: None)
    assert outcome.kept == (newest, middle)
    assert outcome.evicted == (old,)


def test_prune_never_deletes_the_newest_backup_even_over_budget(tmp_path):
    workspace = tmp_path / "workspace"
    name = "1.4.0-20261123-000000"
    _seed_backups(workspace, [name], payload=b"huge" * 100)
    outcome = prune_backups(workspace, keep=1, max_bytes=10, delete=lambda path: None)
    assert outcome.kept == (name,)
    assert outcome.evicted == ()
    assert outcome.failed == ()
    assert (workspace / "backups" / name).exists()


def test_prune_defaults_are_pinned():
    # The newest backup is the restore anchor; three copies cover it plus one
    # failed-upgrade cycle and one in-flight or duplicate snapshot. 5 GiB
    # bounds disk use while staying far above any single real snapshot, so the
    # cap never evicts the newest backup in normal use.
    assert RETENTION_KEEP_BACKUPS == 3
    assert RETENTION_MAX_BYTES == 5 * 1024**3


def test_prune_moves_to_trash_when_the_recycle_bin_is_unavailable(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    _seed_backups(workspace, ["1.4.0-20261121-000000", "1.4.0-20261122-000000"])
    monkeypatch.setattr("wenyi_desktop.updates._recycle_delete", lambda path: False)
    outcome = prune_backups(workspace, keep=1)
    evicted = workspace / "backups" / "1.4.0-20261121-000000"
    trashed = workspace / "backups-trash" / "1.4.0-20261121-000000"
    assert outcome.evicted == ("1.4.0-20261121-000000",)
    assert not evicted.exists()
    assert (trashed / "MANIFEST.json").is_file()


def test_prune_uses_the_recycle_bin_when_available(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    _seed_backups(workspace, ["1.4.0-20261121-000000", "1.4.0-20261122-000000"])
    recycled = []

    def recycle(path):
        recycled.append(path)
        return True

    monkeypatch.setattr("wenyi_desktop.updates._recycle_delete", recycle)
    outcome = prune_backups(workspace, keep=1)
    assert outcome.evicted == ("1.4.0-20261121-000000",)
    assert [path.name for path in recycled] == ["1.4.0-20261121-000000"]
    assert not (workspace / "backups-trash").exists()


def test_prune_failure_is_reported_and_keeps_the_backup(tmp_path):
    workspace = tmp_path / "workspace"
    _seed_backups(workspace, ["1.4.0-20261121-000000", "1.4.0-20261122-000000"])

    def denied(path):
        raise OSError("permission denied")

    outcome = prune_backups(workspace, keep=1, delete=denied)
    assert outcome.failed == ("1.4.0-20261121-000000",)
    assert outcome.evicted == ()
    assert (workspace / "backups" / "1.4.0-20261121-000000").exists()


def test_prune_never_removes_the_snapshot_restore_would_use(tmp_path):
    workspace = tmp_path / "workspace"
    interrupted = workspace / "backups" / "1.5.0-20261123-000000"
    interrupted.mkdir(parents=True)
    (interrupted / "catalog.sqlite3").write_bytes(b"partial copy")
    _seed_backups(workspace, ["1.4.0-20261122-000000"])
    # Restore only ever considers the newest directory. Deleting the
    # manifest-less newest one would make a failed upgrade fall back to the
    # older snapshot, so retention must keep it even though it is unusable.
    assert restore_latest_backup(workspace).status == "absent"
    outcome = prune_backups(workspace, keep=1, delete=lambda path: None)
    assert outcome.kept == ("1.5.0-20261123-000000",)
    assert outcome.evicted == ("1.4.0-20261122-000000",)
    assert restore_latest_backup(workspace).status == "absent"


def test_prune_ignores_hidden_entries_and_missing_roots(tmp_path):
    workspace = tmp_path / "workspace"
    outcome = prune_backups(workspace)
    assert outcome.kept == () and outcome.evicted == () and outcome.failed == ()
    _seed_backups(workspace, ["1.4.0-20261121-000000", "1.4.0-20261122-000000"])
    hidden = workspace / "backups" / ".junk"
    hidden.mkdir()
    (hidden / "catalog.sqlite3").write_bytes(b"junk")
    outcome = prune_backups(workspace, keep=1, delete=lambda path: None)
    assert outcome.kept == ("1.4.0-20261122-000000",)
    assert outcome.evicted == ("1.4.0-20261121-000000",)
    assert hidden.is_dir()


def test_create_context_prunes_upgrade_backups_on_startup(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    names = [f"1.4.0-2026112{day}-000000" for day in range(1, 6)]
    _seed_backups(workspace, names)
    monkeypatch.setattr("wenyi_desktop.updates._recycle_delete", lambda path: False)
    context = create_context(workspace, api_token="test-token")
    try:
        remaining = sorted(entry.name for entry in (workspace / "backups").iterdir())
        assert remaining == names[2:]
        trashed = sorted(entry.name for entry in (workspace / "backups-trash").iterdir())
        assert trashed == names[:2]
    finally:
        context.repository.close()
