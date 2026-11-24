"""Exercise the offline upgrade recovery and restart against temporary SQLite data."""

import hashlib
import json
import os
import queue
import shutil
import sqlite3
import subprocess
import sys
import threading
from contextlib import closing
from importlib.metadata import version
from pathlib import Path

import pytest
from wenyi_core.storage.locks import FileLockHandle
from wenyi_desktop import catalog, paths, updates
from wenyi_desktop.diagnostics import StartupFailure, StartupFailureCode
from wenyi_desktop.main import create_context

ENGINE_VERSION = version("wenyi-desktop")
PROJECT_ID = "0123456789abcdef"


def _copy_snapshot(root, name, created, sources):
    destination = root / updates.BACKUPS_DIR / name
    destination.mkdir(parents=True)
    files = []
    for namespace, source in sources.items():
        contents = source.rglob("*") if source.is_dir() else [source]
        for file in contents:
            if not file.is_file():
                continue
            suffix = file.relative_to(source).as_posix() if source != file else ""
            relative = f"{namespace}/{suffix}" if suffix else namespace
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(file, target)
            files.append(
                {
                    "path": relative,
                    "size": target.stat().st_size,
                    "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                }
            )
    (destination / updates.MANIFEST_NAME).write_text(
        json.dumps(
            {
                "schema_version": 2,
                "source_path": "",
                "created_at": created,
                "version": "1.9.0",
                "files": files,
                "sources": [{"name": name, "path": str(path)} for name, path in sources.items()],
            }
        ),
        encoding="utf-8",
    )
    return destination


def _arm(root, snapshot, *, target=ENGINE_VERSION):
    manifest = snapshot / updates.MANIFEST_NAME
    marker = root / updates.BACKUPS_DIR / updates.ATTEMPT_NAME
    marker.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "snapshot": snapshot.name,
                "target_version": target,
                "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
                "phase": "prepared",
            }
        ),
        encoding="utf-8",
    )
    return marker


def _create_db(path: Path):
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE evidence (value TEXT)")
        connection.execute("INSERT INTO evidence VALUES ('before upgrade')")


def _leave_wal(*paths_):
    # An actual process exit leaves genuine WAL/SHM files without a writer lease.
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import os, sqlite3, sys
connections = []
for path, value in zip(sys.argv[1::2], sys.argv[2::2]):
    connection = sqlite3.connect(path)
    connections.append(connection)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("UPDATE evidence SET value = ?", (value,))
    connection.commit()
os._exit(0)
""",
            *(str(arg) for arg in paths_),
        ],
        check=True,
        timeout=10,
    )


def _value(database):
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
        return connection.execute("SELECT value FROM evidence").fetchone()[0]


def _fixture(tmp_path, *, external=False, snapshot_wal=False):
    root = tmp_path.resolve() / "workspace"
    project = (tmp_path.resolve() / "external" if external else root / "projects") / PROJECT_ID
    project.mkdir(parents=True)
    root.mkdir(exist_ok=True)
    (project / paths.PROJECT_MARKER).write_text(
        json.dumps({"pid": PROJECT_ID, "schema_version": paths.PROJECT_SCHEMA_VERSION}),
        encoding="utf-8",
    )
    database = paths.catalog(root)
    catalog.initialize(database)
    _create_db(database)
    project_db = project / "state.sqlite3"
    _create_db(project_db)
    if snapshot_wal:
        _leave_wal(database, "backup WAL catalog", project_db, "backup WAL project")
    sources = {name: root / name for name in updates.CATALOG_FILES if (root / name).is_file()}
    sources["projects/0"] = project
    backup = _copy_snapshot(root, "1.9.0-first", "2026-11-20T00:00:00Z", sources)
    marker = _arm(root, backup)
    _leave_wal(database, "live WAL catalog", project_db, "live WAL project")
    (project / "only-in-live.txt").write_text("keep me", encoding="utf-8")
    return root, backup, marker, project


def _command(root, *args):
    return [sys.executable, "-m", "wenyi_desktop.desktop", "--data-dir", str(root), *args]


def _startup_env():
    return {
        **os.environ,
        "WENYI_API_TOKEN": "isolated-test-token",
        "WENYI_DESKTOP_VERSION": ENGINE_VERSION,
        "WENYI_DESKTOP_ENGINE_VERSION": ENGINE_VERSION,
        "WENYI_DIAGNOSTICS_PROTOCOL": "1",
    }


@pytest.mark.parametrize("snapshot_wal", [False, True])
def test_offline_command_restores_and_preserves_complete_sqlite_sets(tmp_path, snapshot_wal):
    root, backup, marker, project = _fixture(tmp_path, snapshot_wal=snapshot_wal)
    live_dbs = (paths.catalog(root), project / "state.sqlite3")
    companions = [Path(f"{db}{suffix}") for db in live_dbs for suffix in ("-wal", "-shm")]
    live = {file.relative_to(root): file.read_bytes() for file in (*live_dbs, *companions)}

    pending = subprocess.run(
        _command(root, "--recover-upgrade"), capture_output=True, text=True, timeout=15, check=True
    )
    assert pending.stdout.strip() == backup.name
    subprocess.run(
        _command(root, "--recover-upgrade", backup.name),
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    assert not marker.exists()
    preserved = root / updates.RECOVERY_DIR / backup.name / "preserved"
    for relative, data in live.items():
        # Projects are quarantined under their exact manifest namespace.
        saved = (
            preserved / "projects" / "0" / relative.name
            if relative.parts[0] == "projects"
            else preserved / relative
        )
        assert saved.read_bytes() == data
    assert (preserved / "projects/0/only-in-live.txt").read_text() == "keep me"
    assert not (project / "only-in-live.txt").exists()
    assert [file.exists() for file in companions] == [snapshot_wal] * 4
    assert [_value(db) for db in live_dbs] == (
        ["backup WAL catalog", "backup WAL project"] if snapshot_wal else ["before upgrade"] * 2
    )
    assert updates.recover_upgrade(root, backup.name, ENGINE_VERSION).status == "absent"


@pytest.mark.parametrize("interruption", ["quarantining", "publishing"])
def test_interruption_blocks_real_startup_before_sqlite_then_resumes(
    tmp_path, monkeypatch, interruption
):
    root, backup, marker, project = _fixture(tmp_path, snapshot_wal=True)
    move = updates._move

    def interrupted(source, target, **kwargs):
        result = move(source, target, **kwargs)
        # Simulate power loss after the first rename in either durable phase.
        catalog_moved = target.name == "catalog.sqlite3"
        preserving = target.parent.name == "preserved"
        if catalog_moved and preserving == (interruption == "quarantining"):
            raise OSError("injected crash")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(updates, "_move", interrupted)
        assert updates.recover_upgrade(root, backup.name, ENGINE_VERSION).status == "failed"
    assert json.loads(marker.read_bytes())["phase"] == interruption
    db = paths.catalog(root)
    before = db.read_bytes() if db.exists() else None
    refused = subprocess.run(
        _command(root), input="", env=_startup_env(), capture_output=True, text=True, timeout=15
    )
    assert refused.returncode != 0
    assert "WENYI_READY " not in refused.stdout
    assert 'WENYI_FAILED {"code":"upgrade_recovery_required"}' in refused.stdout
    assert (db.read_bytes() if db.exists() else None) == before
    assert updates.recover_upgrade(root, backup.name, ENGINE_VERSION).status == "restored"
    assert _value(db) == "backup WAL catalog"
    assert _value(project / "state.sqlite3") == "backup WAL project"


def test_staging_error_keeps_the_entire_live_set(tmp_path, monkeypatch):
    root, backup, marker, project = _fixture(tmp_path, snapshot_wal=True)
    live = [paths.catalog(root), root / "catalog.sqlite3-wal", project / "state.sqlite3"]
    before = [file.read_bytes() for file in live]

    def fail_copy(*args):
        raise OSError("injected write failure before publishing the journal")

    with monkeypatch.context() as patch:
        patch.setattr(updates.shutil, "copyfile", fail_copy)
        assert updates.recover_upgrade(root, backup.name, ENGINE_VERSION).status == "failed"
    assert json.loads(marker.read_bytes())["phase"] == "prepared"
    assert [file.read_bytes() for file in live] == before
    assert updates.recover_upgrade(root, backup.name, ENGINE_VERSION).status == "restored"


@pytest.mark.parametrize("relative", ["catalog.sqlite3", "projects/0/state.sqlite3"])
def test_hash_verified_but_corrupt_sqlite_refuses_before_touching_live_data(tmp_path, relative):
    root, backup, marker, project = _fixture(tmp_path)
    damaged = backup / relative
    damaged.write_bytes(damaged.read_bytes()[:100])  # Keep the genuine SQLite header.
    manifest_path = backup / updates.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_bytes())
    for entry in manifest["files"]:
        if entry["path"] == relative:
            entry.update(size=100, sha256=hashlib.sha256(damaged.read_bytes()).hexdigest())
            break
    else:
        pytest.fail("fixture is missing the database to corrupt")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    _arm(root, backup)

    live = [
        Path(f"{db}{suffix}")
        for db in (paths.catalog(root), project / "state.sqlite3")
        for suffix in ("", "-wal", "-shm")
    ]
    before = [file.read_bytes() for file in live]
    outcome = updates.recover_upgrade(root, backup.name, ENGINE_VERSION)
    assert outcome.status == "failed"
    assert outcome.detail and "SQLite integrity check" in outcome.detail
    assert json.loads(marker.read_bytes())["phase"] == "prepared"
    assert [file.read_bytes() for file in live] == before


def test_recovery_checks_hot_journal_on_a_disposable_copy(tmp_path):
    root, _, _, project = _fixture(tmp_path)
    database = project / "state.sqlite3"
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import os, sqlite3, sys
connection = sqlite3.connect(sys.argv[1])
connection.execute("PRAGMA journal_mode=DELETE")
connection.execute("PRAGMA cache_size=5")
connection.execute("CREATE TABLE pages (payload BLOB)")
connection.executemany("INSERT INTO pages VALUES (zeroblob(4096))", [()] * 60)
connection.commit()
connection.execute("UPDATE evidence SET value = 'uncommitted'")
connection.execute("UPDATE pages SET payload = randomblob(4096)")
os._exit(0)
""",
            str(database),
        ],
        check=True,
        timeout=10,
    )
    journal = database.with_name("state.sqlite3-journal")
    contents = journal.read_bytes()
    assert contents.startswith(b"\xd9\xd5\x05\xf9 \xa1c\xd7")  # A synced, hot journal.
    backup = _copy_snapshot(
        root,
        "1.9.0-journal",
        "2026-11-21T00:00:00Z",
        {"catalog.sqlite3": paths.catalog(root), "projects/0": project},
    )
    _arm(root, backup)

    assert updates.recover_upgrade(root, backup.name, ENGINE_VERSION).status == "restored"
    assert journal.read_bytes() == contents
    assert (backup / "projects/0/state.sqlite3-journal").read_bytes() == contents
    assert _value(database) == "live WAL project"  # Opening the restored set rolls back.


def test_recovery_accepts_uninitialized_project_without_opening_its_original(tmp_path):
    root, _, _, project = _fixture(tmp_path)
    db = project / "state.sqlite3"
    assert _value(db) == "live WAL project"  # Close and checkpoint its WAL before removal.
    db.unlink()
    original = project / "original" / "state.sqlite3"
    original.parent.mkdir()
    original.write_bytes(b"Not a project database")
    backup = _copy_snapshot(
        root,
        "1.9.0-uninitialized",
        "2026-11-21T00:00:00Z",
        {"catalog.sqlite3": paths.catalog(root), "projects/0": project},
    )
    _arm(root, backup)

    assert updates.recover_upgrade(root, backup.name, ENGINE_VERSION).status == "restored"
    assert original.read_bytes() == b"Not a project database"
    assert not db.exists()


def test_successful_startup_retires_attempt_before_ready(tmp_path):
    root, backup, marker, _ = _fixture(tmp_path)
    process = subprocess.Popen(
        _command(root),
        env=_startup_env(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    lines: queue.Queue[str] = queue.Queue()
    assert process.stdout is not None
    stdout = process.stdout

    def read():
        for line in stdout:
            lines.put(line)
        lines.put("")

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        for _ in range(100):
            line = lines.get(timeout=15)
            assert line, "the engine exited without becoming ready"
            if line.startswith("WENYI_READY "):
                break
        else:
            pytest.fail("the engine did not become ready")
        assert not marker.exists()
        assert backup.is_dir()
        active = updates.recover_upgrade(root, backup.name, ENGINE_VERSION)
        assert active.status == "failed"
        assert active.detail and "Close every Wenyi process" in active.detail
    finally:
        assert process.stdin is not None
        process.stdin.close()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=15)
        reader.join(timeout=2)
        stdout.close()
        assert process.stderr is not None
        process.stderr.close()
    assert updates.recover_upgrade(root, backup.name, ENGINE_VERSION).status == "absent"
    assert _value(paths.catalog(root)) == "live WAL catalog"


def test_missing_binding_wrong_id_wrong_version_and_wrong_manifest_do_not_restore(tmp_path):
    root, backup, marker, _ = _fixture(tmp_path)
    stray = _copy_snapshot(
        root, "99.0.0-stray", "2026-11-21T00:00:00Z", {"catalog.sqlite3": paths.catalog(root)}
    )
    assert updates.recover_upgrade(root, stray.name, ENGINE_VERSION).status == "absent"
    assert updates.recover_upgrade(root, backup.name, "99.0.0").status == "failed"
    assert not updates.upgrade_recovery_available(root, "99.0.0")
    assert updates.upgrade_recovery_available(root, ENGINE_VERSION)
    (backup / updates.MANIFEST_NAME).write_bytes((stray / updates.MANIFEST_NAME).read_bytes())
    assert updates.recover_upgrade(root, backup.name, ENGINE_VERSION).status == "failed"
    marker.unlink()
    assert updates.recover_upgrade(root, stray.name, ENGINE_VERSION).status == "absent"
    assert _value(paths.catalog(root)) == "live WAL catalog"


@pytest.mark.parametrize("safety", ["external", "spoofed_project", "hardlink", "writer"])
def test_refuses_whole_recovery_if_a_path_or_writer_cannot_be_admitted(tmp_path, safety):
    root, backup, attempt, project = _fixture(tmp_path, external=safety == "external")
    writer = None
    if safety == "spoofed_project":
        (project / paths.PROJECT_MARKER).write_text(
            json.dumps({"pid": "ffffffffffffffff", "schema_version": paths.PROJECT_SCHEMA_VERSION}),
            encoding="utf-8",
        )
    if safety == "hardlink":
        os.link(project / "state.sqlite3", tmp_path / "outside.sqlite3")
    if safety == "writer":
        directory = paths.locks(root) / ".desktop-writers"
        directory.mkdir(parents=True)
        writer = FileLockHandle(directory / ("0" * 32 + "-1.lock"))
        writer.acquire()
    try:
        outcome = updates.recover_upgrade(root, backup.name, ENGINE_VERSION)
    finally:
        if writer is not None:
            writer.release()
    assert outcome.status == "failed", outcome
    assert json.loads(attempt.read_bytes())["phase"] == "prepared"
    assert _value(paths.catalog(root)) == "live WAL catalog"
    assert _value(project / "state.sqlite3") == "live WAL project"


def test_recovery_rejects_linked_backup_and_live_parents(tmp_path):
    root, backup, marker, _ = _fixture(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    moved = other / "projects"
    (root / "projects").rename(moved)
    try:
        (root / "projects").symlink_to(moved, target_is_directory=True)
    except OSError:
        pytest.skip("test host cannot create directory symlinks")
    assert updates.recover_upgrade(root, backup.name, ENGINE_VERSION).status == "failed"
    assert json.loads(marker.read_bytes())["phase"] == "prepared"
    (root / "projects").unlink()
    moved.rename(root / "projects")
    real_backup = other / backup.name
    backup.rename(real_backup)
    backup.symlink_to(real_backup, target_is_directory=True)
    assert updates.recover_upgrade(root, backup.name, ENGINE_VERSION).status == "failed"
    assert _value(paths.catalog(root)) == "live WAL catalog"


def test_retention_uses_verified_creation_order_and_preserves_recovered_snapshot(tmp_path):
    root, restored, _, _ = _fixture(tmp_path)
    assert updates.recover_upgrade(root, restored.name, ENGINE_VERSION).status == "restored"
    sources = {"catalog.sqlite3": paths.catalog(root)}
    older = _copy_snapshot(root, "2.9.0-second", "2026-11-21T00:00:00Z", sources)
    newest = _copy_snapshot(root, "2.10.0-third", "2026-11-22T00:00:00.123456789Z", sources)
    corrupt = _copy_snapshot(root, "99.0.0-fourth", "2026-11-23T00:00:00Z", sources)
    (corrupt / "catalog.sqlite3").write_bytes(b"bad copy")
    idle = root / updates.BACKUPS_DIR / "zz-incomplete"
    idle.mkdir()
    outcome = updates.prune_backups(root, keep=1, max_bytes=1)
    assert outcome.kept == (newest.name, restored.name)
    assert outcome.evicted == (older.name,)
    assert corrupt.is_dir() and idle.is_dir()
    assert restored.is_dir()


def test_catalog_refusal_does_not_consume_prepared_snapshot(tmp_path, monkeypatch):
    root, backup, marker, _ = _fixture(tmp_path)
    catalog_file = paths.catalog(root)
    with closing(sqlite3.connect(catalog_file)) as connection, connection:
        connection.execute("PRAGMA user_version=99")
    monkeypatch.setenv("WENYI_DESKTOP_VERSION", ENGINE_VERSION)
    with pytest.raises(StartupFailure) as caught:
        create_context(root)
    assert caught.value.code == StartupFailureCode.UPGRADE_RECOVERY_REQUIRED
    assert marker.exists()
    assert backup.exists()
    assert _value(catalog_file) == "live WAL catalog"
