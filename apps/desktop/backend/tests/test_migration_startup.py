"""What a start does with the old layout: adopt it once, refusably, and never delete it.

The move itself is covered next door. These tests are about the decision in front
of it, because that decision is what a user actually experiences: the first start
after an upgrade reads their existing data, and every start after that runs on
layer two without touching the old workspace again.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from wenyi_desktop import main, paths
from wenyi_desktop.migration import layers, startup

WORKSPACE = "Wenyi Desktop"


def fingerprint(directory: Path) -> list[tuple[str, int, int]]:
    return sorted(
        (path.name, path.stat().st_size, path.stat().st_mtime_ns)
        for path in directory.rglob("*")
        if path.is_file()
    )


def old_layout(directory: Path) -> Path:
    """A workspace the previous version wrote, in the journal mode it sets."""
    directory.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(directory / "local.sqlite3")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript("""
        CREATE TABLE projects (id TEXT PRIMARY KEY, document TEXT NOT NULL);
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            run_id TEXT NOT NULL UNIQUE, arq_job_id TEXT NOT NULL UNIQUE,
            document TEXT NOT NULL);
        CREATE TABLE exports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            document TEXT NOT NULL);
        CREATE TABLE application_settings (id INTEGER PRIMARY KEY CHECK(id=1), document TEXT NOT NULL);
        CREATE TABLE progress (
            id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
            document TEXT NOT NULL);
        CREATE TABLE desktop_credentials (connection TEXT PRIMARY KEY, document TEXT NOT NULL);
        CREATE TABLE desktop_external_credentials (
            service TEXT PRIMARY KEY, mode TEXT NOT NULL, reference TEXT,
            storage TEXT, revision INTEGER NOT NULL);
    """)
    connection.execute(
        "INSERT INTO projects VALUES (?,?)",
        (
            "0123456789abcdef",
            json.dumps(
                {
                    "id": "0123456789abcdef",
                    "name": "Kept",
                    "title": None,
                    "fmt": None,
                    "source_lang": "en",
                    "target_lang": "zh",
                    "status": "done",
                    "created_at": "2026-11-02T10:30:00+00:00",
                }
            ),
        ),
    )
    connection.execute(
        "INSERT INTO application_settings VALUES (?,?)",
        (1, json.dumps({"template": "标准翻译", "revision": 1, "config": {}})),
    )
    connection.execute(
        "INSERT INTO desktop_credentials VALUES (?,?)",
        ("openai", json.dumps({"mode": "system"})),
    )
    connection.commit()
    connection.close()
    return directory


def test_a_first_start_adopts_the_old_workspace(tmp_path: Path) -> None:
    workspace = old_layout(tmp_path / WORKSPACE)
    before = fingerprint(workspace)
    report = startup.adopt(workspace)
    assert report.ok, report.problems
    assert paths.user_catalog().is_file()
    assert fingerprint(workspace) == before


def test_a_completed_adoption_is_not_repeated(tmp_path: Path) -> None:
    workspace = old_layout(tmp_path / WORKSPACE)
    startup.adopt(workspace)
    migrated_at = json.loads((paths.user_root() / layers.PROVENANCE).read_text(encoding="utf-8"))[
        "migrated_at"
    ]
    # Every later start has to run on layer two. Re-running the move would be
    # harmless, but it would also mean every start reads a database that may by
    # then be owned by a different, older installation.
    startup.adopt(workspace)
    again = json.loads((paths.user_root() / layers.PROVENANCE).read_text(encoding="utf-8"))[
        "migrated_at"
    ]
    assert again == migrated_at


def test_a_failed_adoption_reports_instead_of_raising(tmp_path: Path, monkeypatch) -> None:
    workspace = old_layout(tmp_path / WORKSPACE)

    def fail(self):
        raise OSError("disk full")

    monkeypatch.setattr(layers.Materializer, "database", fail)
    report = startup.adopt(workspace)
    assert not report.ok
    assert "disk full" in report.problems[0]
    # The old workspace is still the complete picture of the user's data.
    assert paths.catalog(workspace).is_file()


def test_an_unfinished_adoption_is_resumed_on_the_next_start(tmp_path: Path, monkeypatch) -> None:
    workspace = old_layout(tmp_path / WORKSPACE)
    original = layers.Materializer.provenance

    def fail(self):
        raise OSError("the machine lost power")

    monkeypatch.setattr(layers.Materializer, "provenance", fail)
    assert not startup.adopt(workspace).ok
    assert layers.pending()
    monkeypatch.setattr(layers.Materializer, "provenance", original)
    assert startup.adopt(workspace).ok
    assert layers.complete(workspace)


def test_a_refusal_travels_to_the_caller(tmp_path: Path) -> None:
    from wenyi_core.storage.locks import FileLockHandle

    workspace = old_layout(tmp_path / WORKSPACE)
    handle = FileLockHandle(str(paths.native_instance_lock(workspace)))
    handle.acquire(blocking=False)
    try:
        # The old application is still writing the data. This is not a state the
        # new version can wait out, so it is raised rather than reported.
        with pytest.raises(layers.MigrationBusy):
            startup.adopt(workspace)
    finally:
        handle.release()


def test_a_new_installation_writes_nothing_into_layer_two(tmp_path: Path) -> None:
    workspace = tmp_path / "fresh"
    workspace.mkdir()
    report = startup.adopt(workspace)
    assert report.ok
    assert not paths.user_root().exists()


def test_a_started_context_reads_the_migrated_data(tmp_path: Path) -> None:
    workspace = old_layout(tmp_path / WORKSPACE)
    context = main.create_context(workspace, migrate=False)
    context.repository.close()
    context = main.create_context(workspace)
    try:
        # The project the old version recorded is what the new one lists.
        assert [row["name"] for row in context.repository.list_projects()] == ["Kept"]
        assert context.repository.database == paths.user_catalog()
    finally:
        context.repository.close()


def test_the_old_catalog_is_not_used_again_after_the_move(tmp_path: Path) -> None:
    workspace = old_layout(tmp_path / WORKSPACE)
    before = fingerprint(workspace)
    context = main.create_context(workspace)
    try:
        context.repository.create_project("New", "en", "zh", {})
    finally:
        context.repository.close()
    # New writes go to layer two only. The old workspace keeps exactly the files
    # it had, which is what makes going back to the previous version work.
    assert fingerprint(workspace) == before


def test_a_second_workspace_is_adopted_even_though_layer_two_is_shared(tmp_path: Path) -> None:
    first = old_layout(tmp_path / "first" / WORKSPACE)
    second = old_layout(tmp_path / "second" / WORKSPACE)
    assert startup.adopt(first).ok
    # Layer two is one directory per machine, so a record that only says "a move
    # happened" is not an answer to "was this workspace moved?". Judging by the
    # record alone would leave the second workspace permanently unadopted.
    assert not layers.complete(second)
    assert startup.adopt(second).ok
    assert layers.complete(second)
    assert json.loads((paths.user_root() / layers.PROVENANCE).read_text(encoding="utf-8"))[
        "source"
    ] == str(paths.catalog(second))


def test_an_unreadable_record_reopens_the_question(tmp_path: Path) -> None:
    workspace = old_layout(tmp_path / WORKSPACE)
    startup.adopt(workspace)
    (paths.user_root() / layers.PROVENANCE).write_text("not json", encoding="utf-8")
    # A record that cannot be read cannot be trusted to describe a finished
    # move, so the next start redoes the (idempotent) work.
    assert not layers.complete(workspace)
    assert startup.adopt(workspace).ok
    assert layers.complete(workspace)


def test_the_workspace_directory_is_unchanged_even_when_it_had_a_lock(tmp_path: Path) -> None:
    workspace = old_layout(tmp_path / WORKSPACE)
    # A workspace that has been opened once has the engine's lock file. Taking
    # that lock must not create anything new and must not change what is there.
    paths.instance_lock(workspace).write_bytes(b"")
    before = fingerprint(workspace)
    startup.adopt(workspace)
    assert fingerprint(workspace) == before


def test_an_open_workspace_blocks_the_move(tmp_path: Path) -> None:
    from wenyi_core.storage.locks import FileLockHandle

    workspace = old_layout(tmp_path / WORKSPACE)
    path = paths.instance_lock(workspace)
    path.write_bytes(b"")
    handle = FileLockHandle(str(path))
    handle.acquire(blocking=False)
    try:
        with pytest.raises(layers.MigrationBusy, match="open in another window"):
            startup.adopt(workspace)
    finally:
        handle.release()
    assert not paths.user_catalog().exists()
