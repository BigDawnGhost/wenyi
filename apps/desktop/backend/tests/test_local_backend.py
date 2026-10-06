"""Desktop catalog uses isolated temporary workspaces and no external services."""

import inspect
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from contextvars import copy_context
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from wenyi_backend import dal, paths
from wenyi_backend.context import current_context, use_context
from wenyi_backend.global_settings import load_settings, save_settings
from wenyi_desktop.local_backend import LocalBackend
from wenyi_desktop.main import create_context


@pytest.fixture
def backend(tmp_path, monkeypatch):
    context = create_context(tmp_path / "desktop")
    value = context.repository
    # Core SqliteStorage is tested separately; catalog tests do not depend on its schema.
    stores = {}

    def store(pid, *, create=True):
        return stores.setdefault(
            pid,
            SimpleNamespace(
                exists=lambda: False,
                lock=lambda **kwargs: nullcontext(),
                list_events=lambda **kwargs: [],
                close=lambda: None,
            ),
        )

    monkeypatch.setattr(value, "storage_for", store)
    try:
        with use_context(context):
            yield value
    finally:
        value.close()


def new_project():
    return dal.create_project("Sample", "en", "zh", {"template": "标准翻译"})


def test_strategy_and_admission_calls_match_repository(backend):
    inspect.signature(backend.set_project_strategy).bind("project", {"steps": {}}, connection=None)
    inspect.signature(backend.admit_job).bind(
        "project",
        "translation",
        "job",
        project_status="translating",
        params={},
        config_snapshot={},
        run_id="run",
    )


def test_catalog_durability_and_workspace_isolation(backend, tmp_path):
    pid = new_project()
    assert Path(paths.project_dir(pid)) == backend.workspace / "projects" / pid
    dal.set_project_config(pid, {"pipeline": {"polish": False}})
    identity = dal.create_job(pid, "translation", "task", run_id="run")
    dal.set_job_status(identity, "error", "offline", result={"stage": "translate"})
    saved = dal.get_job(identity)
    assert saved["run_id"] == "run"
    assert dal.latest_resumable_job(pid) == saved
    workspace = backend.workspace
    backend.close()
    other = create_context(tmp_path / "second")
    try:
        with use_context(other):
            assert other.repository.all_jobs() == []
            assert dal.list_projects() == []
    finally:
        other.repository.close()
    reopened = LocalBackend(workspace)
    try:
        assert reopened.get_job(identity) == saved
        assert reopened.get_project_config(pid) == {"pipeline": {"polish": False}}
    finally:
        reopened.close()


def test_run_identity_and_conditional_transitions(backend):
    import sqlite3

    pid = new_project()
    identity = dal.create_job(pid, "parse", "task", run_id="unique")
    with pytest.raises(sqlite3.IntegrityError):
        dal.create_job(pid, "parse", "other-task", run_id="unique")
    assert backend.set_job_status(identity, "running", expected_status="queued")
    assert not backend.set_job_status(identity, "done", expected_status="queued")
    assert dal.get_job(identity)["status"] == "running"
    assert backend.set_job_status(identity, "interrupted", expected_status={"running"})
    assert dal.get_job_by_arq_id("task")["status"] == "interrupted"


def test_catalog_transaction_rolls_back_project_and_config(backend):
    from wenyi_backend.global_settings import registry_guard

    with pytest.raises(RuntimeError):
        with registry_guard() as connection:
            pid = dal.create_project("Rollback", "en", "zh", {}, connection=connection)
            dal.set_project_config(pid, {"test": True}, connection=connection)
            raise RuntimeError("abort")
    assert dal.list_projects() == []


def test_defaults_do_not_load_cli_config_and_revision_is_optimistic(backend, tmp_path, monkeypatch):
    import yaml
    from wenyi_backend.config_documents import global_document

    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("source_lang: invalid-cli-config", encoding="utf-8")
    initial = load_settings()
    document = yaml.safe_dump(global_document(initial.config))
    saved = save_settings(document, initial.default_template, 0)
    assert saved.revision == 1
    with pytest.raises(HTTPException) as error:
        save_settings(document, initial.default_template, 0)
    assert error.value.status_code == 409
    assert load_settings().revision == 1


def test_export_retention_and_project_ownership(backend):
    pid, other = new_project(), new_project()

    def publish(index):
        identity = dal.create_export(pid, "txt", {"index": index})
        output = Path(paths.exports_dir(pid)) / f"{identity}.txt"
        output.write_text(str(index), encoding="utf-8")
        backend.publish_export(pid, identity, str(output))
        return identity, output

    first, first_path = publish(0)
    stream, _ = backend.open_export(pid, first)
    try:
        with ThreadPoolExecutor(max_workers=3) as executor:
            pending = [executor.submit(copy_context().run, publish, index) for index in range(1, 8)]
            for future in pending:
                future.result()
        assert len(backend.list_exports(pid)) == 5
        assert stream.read() == b"0"
    finally:
        stream.close()
    assert not first_path.exists()
    newest = backend.list_exports(pid)[0]["id"]
    with pytest.raises(FileNotFoundError):
        backend.open_export(other, newest)
    source = backend.project_dir(pid) / "source.txt"
    source.write_text("original", encoding="utf-8")
    with pytest.raises(ValueError):
        backend.publish_export(pid, newest, str(source))
    assert source.read_text() == "original"


@pytest.mark.parametrize("retry_cleanup", [False, True])
def test_retired_export_waits_for_last_stream_and_cleans_on_close(
    backend, monkeypatch, retry_cleanup
):
    pid = new_project()
    identity, output = _export_file(backend, pid)
    first, _ = backend.open_export(pid, identity)
    second, _ = backend.open_export(pid, identity)
    original_unlink = Path.unlink
    attempted = []

    def unlink(path, *args, **kwargs):
        if path == output:
            attempted.append(path)
            # Model Windows deny-delete handles on every test platform.
            if not first.closed or not second.closed:
                raise PermissionError("Export still has an open download")
            if retry_cleanup and len(attempted) == 1:
                raise PermissionError("Temporary filesystem failure after closing")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    try:
        for _ in range(5):
            _export_file(backend, pid)
        assert len(backend.list_exports(pid)) == 5
        assert backend._get("exports", identity)["status"] == "deleting"
        with pytest.raises(FileNotFoundError):
            backend.open_export(pid, identity)
        assert output.exists()
        assert attempted == []
        first.close()
        first.close()
        assert output.exists()
        assert second.read() == str(identity).encode()
        assert attempted == []
    finally:
        first.close()
        second.close()
    assert attempted == [output]
    if retry_cleanup:
        assert output.exists()
        assert backend._get("exports", identity)["status"] == "deleting"
        backend.recover_export_cleanup(pid)
        assert attempted == [output, output]
    assert not output.exists()
    assert backend._get("exports", identity) is None


@pytest.mark.parametrize("failure", ["_export_history_lock", "_cleanup_exports"])
def test_export_close_releases_memory_even_when_cleanup_cannot_run(backend, monkeypatch, failure):
    pid = new_project()
    identity, output = _export_file(backend, pid)
    stream, _ = backend.open_export(pid, identity)
    for _ in range(5):
        _export_file(backend, pid)

    def unavailable(*args):
        raise OSError("Project directory or catalog is temporarily unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(backend, failure, unavailable)
        # Cleanup failure must neither replace the response outcome nor leave a
        # permanent in-memory lease. Concurrent/repeated closes remain harmless.
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: stream.close(), range(2)))
    assert stream.closed
    assert identity not in backend._export_leases
    assert output.exists()
    assert backend._get("exports", identity)["status"] == "deleting"
    backend.recover_export_cleanup(pid)
    assert not output.exists()
    assert backend._get("exports", identity) is None


def test_slow_export_cleanup_does_not_block_other_project(backend, monkeypatch):
    from threading import Event

    from wenyi_desktop import local_backend as module

    pid, other = new_project(), new_project()
    for index in range(5):
        identity = backend.create_export(pid, "html", {})
        output = Path(paths.exports_dir(pid)) / f"{identity}.html"
        output.write_text(str(index))
        output.with_suffix(".assets").mkdir()
        backend.publish_export(pid, identity, str(output))
    identity = backend.create_export(pid, "html", {})
    output = Path(paths.exports_dir(pid)) / f"{identity}.html"
    output.write_text("new")
    entered, release = Event(), Event()
    original = module.shutil.rmtree

    def slow_remove(path):
        entered.set()
        assert release.wait(10)
        original(path)

    monkeypatch.setattr(module.shutil, "rmtree", slow_remove)
    with ThreadPoolExecutor(max_workers=2) as executor:
        publication = executor.submit(backend.publish_export, pid, identity, str(output))
        try:
            assert entered.wait(5)
            assert backend.get_project_config(other) == {}
            backend.set_project_status(other, "translated")
            assert backend._get("projects", other)["status"] == "translated"
        finally:
            release.set()
        publication.result()


def _export_file(backend, pid, *, publish=True, fmt="txt"):
    identity = backend.create_export(pid, fmt, {})
    output = Path(paths.exports_dir(pid)) / f"{identity}.{fmt}"
    output.write_text(str(identity), encoding="utf-8")
    if publish:
        backend.publish_export(pid, identity, str(output))
    return identity, output


def test_catalog_reads_use_wal_snapshot_while_writer_is_active(backend):
    pid = new_project()
    with backend.transaction() as connection:
        backend.update_project(pid, connection=connection, name="uncommitted")
        with ThreadPoolExecutor(max_workers=1) as executor:
            assert executor.submit(backend._get, "projects", pid).result(2)["name"] == "Sample"
            assert len(executor.submit(backend.list_exports, pid).result(2)) == 0
    assert backend._get("projects", pid)["name"] == "uncommitted"


@pytest.mark.parametrize("recovery", ["explicit", "publish"])
def test_failed_removal_leaves_retryable_hidden_tombstone(backend, monkeypatch, recovery):
    pid = new_project()
    old_id, old_file = _export_file(backend, pid)
    for _ in range(4):
        _export_file(backend, pid)
    original = Path.unlink

    def denied(path, *args, **kwargs):
        if path == old_file:
            raise PermissionError("temporarily open on Windows")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", denied)
        _export_file(backend, pid)
    assert old_file.exists()
    assert backend._get("exports", old_id)["status"] == "deleting"
    assert len(backend.list_exports(pid)) == 5
    with pytest.raises(FileNotFoundError):
        backend.open_export(pid, old_id)
    backend.set_export_status(old_id, "done", path=str(old_file))
    assert backend._get("exports", old_id)["status"] == "deleting"
    if recovery == "explicit":
        backend.recover_export_cleanup(pid)
    else:
        _export_file(backend, pid)
    assert not old_file.exists()
    assert backend._get("exports", old_id) is None
    assert len(list(old_file.parent.glob("*.txt"))) == 5


@pytest.mark.parametrize("phase", ["publication", "cleanup"])
@pytest.mark.parametrize("failure", ["sql", "commit"])
def test_export_sql_failure_never_restores_deleted_file_as_done(
    backend, monkeypatch, phase, failure
):
    import sqlite3

    from wenyi_desktop.local_backend import LocalBackend

    pid = new_project()
    old_id, old_file = _export_file(backend, pid)
    for _ in range(4):
        _export_file(backend, pid)
    new_id, new_file = _export_file(backend, pid, publish=False)
    connect = sqlite3.connect

    class FaultConnection(sqlite3.Connection):
        affected = False

        def execute(self, sql, parameters=()):
            if (phase == "cleanup" and sql.startswith("DELETE FROM exports")) or (
                phase == "publication"
                and sql.startswith("UPDATE exports")
                and '"status": "deleting"' in parameters[0]
            ):
                self.affected = True
                if failure == "sql":
                    raise sqlite3.OperationalError("injected SQL failure")
            return super().execute(sql, parameters)

        def commit(self):
            if self.affected and failure == "commit":
                raise sqlite3.OperationalError("injected commit failure")
            return super().commit()

    with monkeypatch.context() as patch:
        patch.setattr(
            sqlite3, "connect", lambda *a, **kw: connect(*a, **kw, factory=FaultConnection)
        )
        with pytest.raises(sqlite3.OperationalError, match="injected"):
            backend.publish_export(pid, new_id, str(new_file))
    # Reopen the catalog as after process restart; no in-memory recovery assumptions.
    reopened = LocalBackend(str(backend.workspace))
    old = reopened._get("exports", old_id)
    assert old["status"] == ("done" if phase == "publication" else "deleting")
    assert old_file.exists() == (phase == "publication")
    if phase == "publication":
        assert reopened._get("exports", new_id)["status"] == "pending"
        reopened.publish_export(pid, new_id, str(new_file))
    else:
        with pytest.raises(FileNotFoundError):
            reopened.open_export(pid, old_id)
        reopened.recover_export_cleanup(pid)
    assert reopened._get("exports", old_id) is None
    assert len(reopened.list_exports(pid)) == 5


def test_interruption_after_unlink_keeps_committed_tombstone(backend, monkeypatch):
    from wenyi_desktop.local_backend import LocalBackend

    pid = new_project()
    old_id, old_file = _export_file(backend, pid)
    for _ in range(4):
        _export_file(backend, pid)
    new_id, new_file = _export_file(backend, pid, publish=False)
    unlink = Path.unlink

    def interrupted(path, *args, **kwargs):
        result = unlink(path, *args, **kwargs)
        if path == old_file:
            raise KeyboardInterrupt
        return result

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", interrupted)
        with pytest.raises(KeyboardInterrupt):
            backend.publish_export(pid, new_id, str(new_file))
    reopened = LocalBackend(str(backend.workspace))
    assert not old_file.exists()
    assert reopened._get("exports", old_id)["status"] == "deleting"
    with pytest.raises(FileNotFoundError):
        reopened.open_export(pid, old_id)
    reopened.recover_export_cleanup(pid)
    assert reopened._get("exports", old_id) is None


def _crash_during_export_cleanup(workspace, pid, new_id, new_file, old_file, after_unlink):
    import os

    from wenyi_desktop.local_backend import LocalBackend

    original = Path.unlink

    def crash(path, *args, **kwargs):
        if str(path) == old_file:
            if after_unlink:
                original(path, *args, **kwargs)
            os._exit(23)
        return original(path, *args, **kwargs)

    Path.unlink = crash
    LocalBackend(workspace).publish_export(pid, new_id, new_file)


@pytest.mark.parametrize("after_unlink", [False, True])
def test_process_exit_during_cleanup_is_recoverable(backend, after_unlink):
    import multiprocessing

    pid = new_project()
    old_id, old_file = _export_file(backend, pid)
    for _ in range(4):
        _export_file(backend, pid)
    new_id, new_file = _export_file(backend, pid, publish=False)
    child = multiprocessing.get_context("spawn").Process(
        target=_crash_during_export_cleanup,
        args=(str(backend.workspace), pid, new_id, str(new_file), str(old_file), after_unlink),
    )
    child.start()
    child.join(15)
    if child.is_alive():
        child.terminate()
        child.join(5)
        pytest.fail("Export cleanup child did not exit")
    assert child.exitcode == 23
    assert backend._get("exports", old_id)["status"] == "deleting"
    assert old_file.exists() is not after_unlink
    with pytest.raises(FileNotFoundError):
        backend.open_export(pid, old_id)
    backend.recover_export_cleanup(pid)
    assert backend._get("exports", old_id) is None
    assert len(list(old_file.parent.glob("*.txt"))) == 5


def test_download_open_and_retention_share_only_project_history_lock(backend, monkeypatch):
    from threading import Event

    from wenyi_desktop import local_export_files

    pid, other = new_project(), new_project()
    old_id, old_file = _export_file(backend, pid)
    for _ in range(4):
        _export_file(backend, pid)
    new_id, new_file = _export_file(backend, pid, publish=False)
    entered, release = Event(), Event()
    path_open = local_export_files.open_regular

    def delayed_open(path, *args, **kwargs):
        if path == old_file:
            entered.set()
            assert release.wait(10)
        return path_open(path, *args, **kwargs)

    monkeypatch.setattr(local_export_files, "open_regular", delayed_open)
    with ThreadPoolExecutor(max_workers=2) as executor:
        download = executor.submit(backend.open_export, pid, old_id)
        try:
            assert entered.wait(5)
            publication = executor.submit(backend.publish_export, pid, new_id, str(new_file))
            backend.set_project_status(other, "translated")
            assert not publication.done()
            assert old_file.exists()
        finally:
            release.set()
        stream, _ = download.result()
        try:
            publication.result(5)
            assert stream.read() == str(old_id).encode()
        finally:
            stream.close()
    with pytest.raises(FileNotFoundError):
        backend.open_export(pid, old_id)


@pytest.mark.parametrize("unsafe", ["source", "outside", "symlink", "assets_symlink"])
def test_retention_refuses_unsafe_paths(backend, tmp_path, unsafe):
    pid = new_project()
    old_id, old_file = _export_file(backend, pid, fmt="html")
    protected = (
        backend.project_dir(pid) / "source.html"
        if unsafe == "source"
        else tmp_path / "outside.html"
    )
    protected.write_text("original")
    document = backend._get("exports", old_id)
    if unsafe in {"source", "outside"}:
        document["path"] = str(protected)
        backend._put("exports", old_id, document)
    elif unsafe == "symlink":
        old_file.unlink()
        old_file.symlink_to(protected)
    else:
        assets = tmp_path / "protected.assets"
        assets.mkdir()
        (assets / "image").write_text("image")
        old_file.with_suffix(".assets").symlink_to(assets, target_is_directory=True)
    for _ in range(5):
        _export_file(backend, pid)
    backend.recover_export_cleanup(pid)
    assert protected.read_text() == "original"
    assert old_file.exists()
    assert backend._get("exports", old_id)["status"] == "deleting"
    if unsafe == "assets_symlink":
        assert (assets / "image").read_text() == "image"


@pytest.mark.parametrize("status", ["pending", "running"])
def test_delete_project_rejects_active_export(backend, status):
    pid = new_project()
    identity = backend.create_export(pid, "txt", {})
    backend.set_export_status(identity, status)
    with pytest.raises(HTTPException) as error:
        backend.delete_project(pid)
    assert error.value.status_code == 409
    assert backend._get("projects", pid)
    backend.set_export_status(identity, "error", error="stopped")
    backend.delete_project(pid)
    assert backend._get("projects", pid) is None


def test_settings_projects_health_and_download_routes_without_pg(backend):
    from wenyi_desktop.main import create_app

    client = TestClient(create_app(context=current_context()))
    pid = new_project()
    assert client.get("/health").json() == {"status": "ok", "db": "ok"}
    assert client.get("/projects").json()[0]["id"] == pid
    assert client.get(f"/projects/{pid}").json()["chapter_count"] == 0
    defaults = client.get("/settings/defaults")
    assert defaults.status_code == 200
    body = {key: defaults.json()[key] for key in ("yaml", "default_template", "revision")}
    assert client.post("/settings/validate", json=body).status_code == 200
    assert client.put("/settings", json=body).status_code == 200
    assert client.put("/settings", json=body).status_code == 409
    eid = dal.create_export(pid, "txt", {})
    output = Path(paths.exports_dir(pid)) / "sample.txt"
    output.write_text("download", encoding="utf-8")
    backend.publish_export(pid, eid, str(output))
    assert client.get(f"/projects/{pid}/exports").json()[0]["id"] == eid
    assert client.get(f"/projects/{pid}/exports/{eid}/download").text == "download"
    assert client.delete(f"/projects/{pid}").status_code == 200
    assert output.exists()
    assert client.get(f"/projects/{pid}").status_code == 404


def test_backend_cannot_be_replaced_implicitly(backend, tmp_path):
    original = current_context()
    other = create_context(tmp_path / "other")
    try:
        assert current_context() is original
        with use_context(other):
            assert current_context().repository is other.repository
        assert current_context().repository is backend
    finally:
        other.repository.close()


def test_atomic_admission_and_late_compensation(backend):
    pid = new_project()
    identity = backend.admit_job(pid, "parse", "first", project_status="parsing")
    assert dal.get_project(pid)["status"] == "parsing"
    with pytest.raises(HTTPException) as error:
        backend.admit_job(pid, "parse", "second", project_status="parsing")
    assert error.value.status_code == 409
    assert len(dal.list_jobs(pid)) == 1
    assert backend.fail_admission(identity, "uploaded", "queue rejected")
    assert dal.get_project(pid)["status"] == "uploaded"
    second = backend.admit_job(pid, "parse", "second", project_status="parsing")
    backend.set_job_status(second, "running", expected_status="queued")
    assert not backend.fail_admission(second, "uploaded", "late rejection")
    assert dal.get_project(pid)["status"] == "parsing"
    assert dal.get_job(second)["status"] == "running"


def test_export_admission_is_atomic_and_late_compensation_is_ignored(backend):
    import sqlite3

    pid = new_project()
    export_id, job_id = backend.admit_export(pid, "txt", {}, "unique", {})
    with pytest.raises(sqlite3.IntegrityError):
        backend.admit_export(pid, "txt", {}, "unique", {})
    assert len(backend.list_exports(pid)) == 1
    assert backend.fail_export_admission(job_id, "queue unavailable")
    assert backend.list_exports(pid)[0]["id"] == export_id
    assert backend.list_exports(pid)[0]["status"] == "error"
    _, newer = backend.admit_export(pid, "txt", {}, "newer", {})
    backend.set_job_status(newer, "running", expected_status="queued")
    assert not backend.fail_export_admission(newer, "late failure")


def test_model_rename_updates_projects_not_frozen_jobs(backend):
    import json

    from wenyi_backend.model_registry import rename_model_references
    from wenyi_desktop.main import create_app

    client = TestClient(create_app(context=current_context()))
    pid = new_project()
    settings = client.get("/settings").json()
    document = settings["effective"]
    old = document["llm"]["tiers"]["strong"]
    response = client.put(
        f"/projects/{pid}/config",
        json={"yaml": json.dumps({"llm": {"tiers": {"strong": old}}})},
    )
    assert response.status_code == 200, response.text
    job_id = dal.create_job(pid, "translation", "frozen", config_snapshot=document)
    frozen = dal.get_job(job_id)["params"]
    llm = document["llm"]
    llm["preset"] = None
    llm["models"]["renamed"] = llm["models"].pop(old)
    document["llm"] = rename_model_references(llm, {old: "renamed"})
    body = {
        "yaml": json.dumps(document),
        "revision": 0,
        "default_template": settings["default_template"],
        "model_renames": {old: "renamed"},
    }
    assert client.post("/settings/validate", json=body).status_code == 200
    assert dal.get_project_config(pid)["llm"]["tiers"]["strong"] == old
    response = client.put("/settings", json=body)
    assert response.status_code == 200, response.text
    assert dal.get_project_config(pid)["llm"]["tiers"]["strong"] == "renamed"
    assert dal.get_job(job_id)["params"] == frozen


def test_local_lifespan_starts_runner_without_pg_and_closes_backend(backend, monkeypatch):
    from wenyi_desktop import main

    calls = []

    async def start():
        calls.append("start")

    async def stop():
        calls.append("stop")

    runtime = SimpleNamespace(start=start, stop=stop, accepting=True)
    context = current_context()
    closed = []
    close = backend.close

    def close_backend():
        closed.append(True)
        close()

    monkeypatch.setattr(backend, "close", close_backend)
    monkeypatch.setattr(main, "LocalRuntime", lambda catalog, owner: runtime)
    with TestClient(main.create_app(context=context)) as client:
        assert client.get("/health").status_code == 200
        assert calls == ["start"]
    assert calls == ["start", "stop"]
    assert context.telemetry.runtime is None
    assert closed == [True]


def test_progress_is_latest_only_and_cascades_with_project(backend):
    pid = new_project()
    assert backend.load_progress(pid) is None
    backend.save_progress(pid, {"run_id": "run", "percent": 10})
    backend.save_progress(pid, {"run_id": "run", "percent": 100})
    assert backend.load_progress(pid) == {"run_id": "run", "percent": 100}
    dal.delete_project(pid)
    assert backend.load_progress(pid) is None


@pytest.mark.parametrize("failure", [False, True])
def test_project_read_opens_existing_storage_only_and_always_closes(backend, monkeypatch, failure):
    pid = new_project()
    calls = []

    def manifest():
        if failure:
            raise ValueError("Unreadable manifest")
        return {"title": "Stored title", "fmt": "txt"}

    def storage(project_id, *, create):
        calls.append((project_id, create))
        return SimpleNamespace(
            exists=lambda: True,
            load_manifest=manifest,
            close=lambda: calls.append("closed"),
        )

    monkeypatch.setattr(backend, "storage_for", storage)
    if failure:
        with pytest.raises(ValueError, match="Unreadable manifest"):
            backend.get_project(pid)
    else:
        assert backend.get_project(pid)["title"] == "Stored title"
    assert calls == [(pid, False), "closed"]
