"""Catalog repair and import use only synthetic projects in temporary workspaces."""

import json
import sqlite3
from pathlib import Path

import pytest
from catalog_support import orphan
from fastapi.testclient import TestClient
from wenyi_backend.context import use_context
from wenyi_desktop import catalog_recovery, paths
from wenyi_desktop.local_runtime import LocalRuntime
from wenyi_desktop.main import create_app, create_context


def with_jobs(directory: Path, *rows: tuple[str, str, str | None]) -> None:
    """Give one project directory the scheduling table an older build wrote."""
    with sqlite3.connect(directory / "state.sqlite3") as connection:  # noqa: SIM117
        connection.execute(
            "CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "kind TEXT NOT NULL, status TEXT NOT NULL, error TEXT)"
        )
        connection.executemany("INSERT INTO jobs(kind,status,error) VALUES (?,?,?)", rows)


def run_recovery(context, *, startup=True):
    import asyncio

    from wenyi_backend.context import use_context

    async def run():
        with use_context(context):
            LocalRuntime(context.repository, context).recover(startup=startup)

    asyncio.run(run())


@pytest.fixture
def windows(monkeypatch):
    from wenyi_desktop import local_runtime

    monkeypatch.setattr(local_runtime, "platform", "win32", raising=False)


def test_a_directory_without_state_reports_the_missing_file(tmp_path):
    directory = tmp_path / "some folder"
    directory.mkdir()
    project, reason = catalog_recovery.scan_project(
        directory, strict=True, defaults=create_context(tmp_path).repository.defaults()
    )
    assert project is None
    assert reason == catalog_recovery.STATE_MISSING


def test_unreadable_state_and_invalid_metadata_are_told_apart(tmp_path):
    defaults = create_context(tmp_path).repository.defaults()
    corrupt, _, _ = orphan(tmp_path, "c" * 16)
    (corrupt / "state.sqlite3").write_bytes(b"not a database")
    reason = catalog_recovery.scan_project(corrupt, strict=True, defaults=defaults)[1]
    assert reason == catalog_recovery.STATE_UNREADABLE

    invalid, _, _ = orphan(tmp_path, "d" * 16)
    with sqlite3.connect(invalid / "state.sqlite3") as connection:
        connection.execute("UPDATE artifacts SET value='[]' WHERE key='manifest.json'")
    reason = catalog_recovery.scan_project(invalid, strict=True, defaults=defaults)[1]
    assert reason == catalog_recovery.METADATA_INVALID


def test_a_directory_with_no_original_names_that_it_has_none(tmp_path):
    defaults = create_context(tmp_path).repository.defaults()
    directory, source, _ = orphan(tmp_path, "e" * 16)
    source.unlink()
    project, reason = catalog_recovery.scan_project(directory, strict=True, defaults=defaults)
    assert project is None
    assert reason == catalog_recovery.SOURCE_MISSING


def test_two_originals_that_both_match_are_refused_as_ambiguous(tmp_path):
    defaults = create_context(tmp_path).repository.defaults()
    directory, source, _ = orphan(tmp_path, "f" * 16)
    # Same bytes, two names: only the checksum can tell them apart, and both pass.
    twin = source.with_name(f"source-{'a' * 32}.txt")
    twin.write_bytes(source.read_bytes())
    project, reason = catalog_recovery.scan_project(directory, strict=True, defaults=defaults)
    assert project is None
    assert reason == catalog_recovery.SOURCE_AMBIGUOUS


def test_strict_scan_refuses_what_a_picked_directory_accepts_and_flags(tmp_path):
    directory, source, digest = orphan(tmp_path, "1" * 16)
    defaults = create_context(tmp_path).repository.defaults()
    source.write_text("Rewritten after the run.\n", encoding="utf-8")

    refused, reason = catalog_recovery.scan_project(directory, strict=True, defaults=defaults)
    assert refused is None
    assert reason == catalog_recovery.SOURCE_CHANGED

    adopted, failure = catalog_recovery.scan_project(directory, strict=False, defaults=defaults)
    assert failure is None
    assert adopted["source"]["source_changed"] is True
    assert adopted["source"]["source_sha256"] != digest


def test_a_matching_directory_is_not_flagged_as_changed(tmp_path):
    directory, _, digest = orphan(tmp_path, "2" * 16)
    project, failure = catalog_recovery.scan_project(
        directory, strict=False, defaults=create_context(tmp_path).repository.defaults()
    )
    assert failure is None
    assert project["source"]["source_changed"] is False
    assert project["source"]["source_sha256"] == digest


@pytest.mark.parametrize("rows,expected", [([("translation", "running", None)], "translating")])
def test_a_recorded_job_names_the_state_the_chapters_cannot(tmp_path, rows, expected):
    # Forward compatibility: a directory that does record jobs is read. The
    # current engine writes none, which the next test pins.
    directory, _, _ = orphan(tmp_path, "3" * 16)
    with_jobs(directory, *rows)
    project, failure = catalog_recovery.scan_project(
        directory, strict=True, defaults=create_context(tmp_path).repository.defaults()
    )
    assert failure is None
    assert project["source"]["status"] == expected


def test_a_current_project_directory_records_no_jobs(tmp_path):
    # The state a lost catalog would have named lives in the catalog, not here.
    # This assertion stops a future batch from reading jobs out of a directory
    # that never holds them; see the module docstring.
    directory, _, _ = orphan(tmp_path, "4" * 16)
    with sqlite3.connect(directory / "state.sqlite3") as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
    assert "jobs" not in tables
    project, failure = catalog_recovery.scan_project(
        directory, strict=True, defaults=create_context(tmp_path).repository.defaults()
    )
    assert failure is None
    assert project["source"]["status"] == "prepared"


def test_chapter_status_decides_when_no_job_survives(tmp_path):
    done, _, _ = orphan(tmp_path, "5" * 16, done=True)
    assert (
        catalog_recovery.scan_project(
            done, strict=True, defaults=create_context(tmp_path).repository.defaults()
        )[0]["source"]["status"]
        == "done"
    )
    pending, _, _ = orphan(tmp_path, "6" * 16)
    assert (
        catalog_recovery.scan_project(
            pending, strict=True, defaults=create_context(tmp_path).repository.defaults()
        )[0]["source"]["status"]
        == "prepared"
    )


def test_recovery_scans_a_root_the_user_pointed_the_application_at(tmp_path, windows):
    # The default root holds nothing; the project lives in the library the
    # settings document names, which is the whole point of scanning candidates.
    external = tmp_path / "Elsewhere"
    directory, _, _ = orphan(external, "7" * 16)
    context = create_context(tmp_path / "workspace")
    backend = context.repository
    with backend.transaction() as connection:
        backend.save_settings(
            {
                "config": {},
                "template": "standard",
                "revision": 1,
                paths.LAST_LIBRARY_ROOT: str(external / "projects"),
            },
            connection,
        )
    run_recovery(context)
    assert [row["id"] for row in context.repository.list_projects()] == [directory.name]


def test_recovery_scans_the_parents_the_workspace_index_remembers(tmp_path, windows):
    external = tmp_path / "Recorded"
    directory, _, _ = orphan(external, "8" * 16)
    index = paths.user_workspace_index()
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text(
        json.dumps({"schema_version": 1, "projects": [{"id": "x", "path": str(directory)}]}),
        encoding="utf-8",
    )
    context = create_context(tmp_path / "workspace")
    run_recovery(context)
    assert [row["id"] for row in context.repository.list_projects()] == [directory.name]


def test_a_root_listed_twice_is_scanned_once(tmp_path, windows):
    external = tmp_path / "Shared"
    orphan(external, "9" * 16)
    nested = external / "nested"
    nested.mkdir()
    context = create_context(tmp_path / "workspace")
    roots = catalog_recovery.candidate_roots(context.repository)
    assert roots[0] == paths.projects(context.repository.workspace)
    assert len({str(root) for root in roots}) == len(roots)


def test_import_adopts_a_picked_directory_and_reports_its_state(tmp_path, windows):
    source_root = tmp_path / "Downloads"
    directory, source, digest = orphan(source_root, "a" * 16)
    app = create_app(tmp_path / "workspace")
    with TestClient(app) as client:
        response = client.post("/desktop/projects/import", json={"path": str(directory)})
        assert response.status_code == 201, response.text
        imported = response.json()
        assert imported["id"] == directory.name
        assert imported["source_sha256"] == digest
        assert imported["status"] == "prepared"
        assert [row["id"] for row in client.get("/projects").json()] == [directory.name]
    moved = tmp_path / "workspace" / "projects" / directory.name / source.name
    assert moved.is_file()
    assert not directory.exists()


def test_import_accepts_a_changed_original_and_marks_it(tmp_path, windows):
    source_root = tmp_path / "Downloads"
    directory, source, digest = orphan(source_root, "b" * 16)
    source.write_text("Edited outside the application.\n", encoding="utf-8")
    app = create_app(tmp_path / "workspace")
    with TestClient(app) as client:
        response = client.post("/desktop/projects/import", json={"path": str(directory)})
        assert response.status_code == 201, response.text
        imported = response.json()
        assert imported["source_changed"] is True
        assert imported["source_sha256"] != digest
        assert imported["status"] == "prepared"


def test_import_refusal_names_what_is_wrong_with_the_folder(tmp_path, windows):
    empty = tmp_path / "NotAProject"
    empty.mkdir()
    app = create_app(tmp_path / "workspace")
    with TestClient(app) as client:
        response = client.post("/desktop/projects/import", json={"path": str(empty)})
        assert response.status_code == 422
        body = response.json()
        assert body["code"] == "project_import_failed"
        assert "state.sqlite3" in body["message"]
        missing = client.post("/desktop/projects/import", json={"path": str(tmp_path / "gone")})
        assert missing.status_code == 422
        assert "no folder" in missing.json()["message"]


def test_import_refuses_a_second_copy_of_a_registered_project(tmp_path, windows):
    # The same identifier in two places is the case a re-import would silently
    # turn into two rows for one book, so the second copy is refused.
    first, _, _ = orphan(tmp_path / "one", "e" * 16)
    second, _, _ = orphan(tmp_path / "two", "e" * 16)
    app = create_app(tmp_path / "workspace")
    with TestClient(app) as client:
        assert client.post("/desktop/projects/import", json={"path": str(first)}).status_code == 201
        again = client.post("/desktop/projects/import", json={"path": str(second)})
        assert again.status_code == 409
        assert again.json()["code"] == "project_import_conflict"
        assert client.get("/projects").json()[0]["id"] == first.name


def test_an_imported_project_passes_the_owning_check_that_deletion_relies_on(tmp_path, windows):
    # Import and deletion have to agree on what a project directory is: a copy
    # the gate would refuse is a copy the rest of the application cannot own.
    directory, source, _ = orphan(tmp_path / "Elsewhere", "f" * 16)
    backend = create_context(tmp_path / "workspace").repository
    stored = catalog_recovery.import_project(backend, directory)
    admitted = backend._admitted_project_directory(
        stored["id"], backend._get("projects", stored["id"])
    )
    assert admitted == (paths.project(backend.workspace, stored["id"])).resolve()
    assert (admitted / source.name).is_file()
    assert (admitted / paths.PROJECT_MARKER).is_file()


def test_import_then_restart_keeps_the_project_and_its_state(tmp_path):
    directory, source, digest = orphan(tmp_path / "Elsewhere", "a" * 16, done=True)
    workspace = tmp_path / "workspace"
    with TestClient(create_app(workspace)) as client:
        assert (
            client.post("/desktop/projects/import", json={"path": str(directory)}).status_code
            == 201
        )
        project = client.get(f"/projects/{directory.name}").json()
        assert project["status"] == "done"
        assert project["initialized"] is True
        assert project["available"] is True
    # The recovered record survives a restart without the directory moving again.
    with TestClient(create_app(workspace)) as client:
        listed = client.get("/projects").json()
        assert [row["id"] for row in listed] == [directory.name]
        assert listed[0]["status"] == "done"
        assert listed[0]["available"] is True
    stored = workspace / "projects" / directory.name / source.name
    assert stored.is_file()


def test_an_imported_project_is_owned_by_the_workspace_it_was_moved_into(tmp_path):
    directory, source, _ = orphan(tmp_path / "Elsewhere", "b" * 16)
    context = create_context(tmp_path / "workspace")
    backend = context.repository
    with use_context(context):
        stored = catalog_recovery.import_project(backend, directory)
        admitted = backend._admitted_project_directory(
            stored["id"], backend._get("projects", stored["id"])
        )
        assert admitted == paths.project(backend.workspace, stored["id"]).resolve()
        assert (admitted / source.name).is_file()
        assert (admitted / paths.PROJECT_MARKER).is_file()


def test_a_file_is_refused_as_a_folder(tmp_path):
    target = tmp_path / "book.txt"
    target.write_text("text", encoding="utf-8")
    with TestClient(create_app(tmp_path / "workspace")) as client:
        response = client.post("/desktop/projects/import", json={"path": str(target)})
        assert response.status_code == 422
        assert "not a folder" in response.json()["message"]


@pytest.mark.parametrize("path", ["", "  ", "relative/folder", "C:\\bad\x00name"])
def test_a_path_that_cannot_be_a_folder_is_refused_before_any_read(tmp_path, path):
    with TestClient(create_app(tmp_path / "workspace")) as client:
        response = client.post("/desktop/projects/import", json={"path": path})
        assert response.status_code == 422
        assert response.json()["code"] == "project_import_failed"


def test_a_folder_without_a_project_identifier_is_adopted_under_a_new_one(tmp_path, windows):
    # A folder copied from another computer can carry any name at all. The
    # identifier is what every later path is derived from, so one is generated
    # and the directory is renamed to carry it.
    directory, source, _ = orphan(tmp_path / "Downloads", done=True)
    plain = directory.with_name("My Imported Book")
    directory.rename(plain)
    app = create_app(tmp_path / "workspace")
    with TestClient(app) as client:
        response = client.post("/desktop/projects/import", json={"path": str(plain)})
        assert response.status_code == 201, response.text
        imported = response.json()
        assert paths.project_id(imported["id"])
        assert imported["status"] == "done"
        projects = tmp_path / "workspace" / "projects"
        stored = next(
            entry for entry in projects.iterdir() if entry.name.startswith(imported["id"])
        )
        # The destination is built from the identifier, which is how the deletion
        # gate and every later path resolve this project.
        assert stored.name == imported["id"]
        assert (stored / source.name).is_file()
        assert (stored / paths.PROJECT_MARKER).is_file()
    assert not plain.exists()
