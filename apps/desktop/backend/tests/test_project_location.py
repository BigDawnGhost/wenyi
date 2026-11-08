"""A chosen project location must never make a project the user already has vanish."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from wenyi_backend.context import use_context
from wenyi_desktop import paths
from wenyi_desktop.library_location import LibraryLocationRefused, admit
from wenyi_desktop.local_backend import LocalBackend
from wenyi_desktop.main import create_app, create_context

PROJECT = "0123456789abcdef"


@pytest.fixture
def roots(tmp_path):
    """A workspace in the old layout and a separate library a person may choose."""
    return tmp_path / "Wenyi Desktop", tmp_path / "Chosen Library"


@pytest.fixture
def backend(roots):
    workspace, library = roots
    value = LocalBackend(workspace, library_root=library)
    try:
        yield value
    finally:
        value.close()


def old_style_project(backend: LocalBackend, pid: str = PROJECT) -> Path:
    """A current-format project explicitly placed in the data-root library."""
    backend.create_project(
        "A default book",
        "en",
        "zh",
        {},
        project_id=pid,
        library_root=paths.projects(backend.workspace),
    )
    directory = paths.project(backend.workspace, pid)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def test_an_existing_project_keeps_the_directory_it_has(backend):
    # The highest-priority promise of this change: choosing a default location
    # must not move, hide, or re-derive a project that already exists. The old
    # flat directory is what `project_dir` has always answered, and it still is.
    directory = old_style_project(backend)
    assert backend.project_dir(PROJECT) == directory
    assert backend.project_dir(PROJECT).is_relative_to(paths.projects(backend.workspace))


def test_both_roots_are_recognised_so_the_old_one_never_drops_out(backend, roots):
    _, library = roots
    workspace = paths.projects(backend.workspace)
    assert backend.project_roots() == (library, workspace)


def test_a_new_project_is_created_in_the_chosen_library(backend, roots):
    _, library = roots
    pid = backend.create_project("A new book", "en", "zh", {})
    directory = backend.project_dir(pid)
    assert directory.parent == library
    # The readable half is for a person; the identifier still leads the name.
    assert paths.split_project_name(directory.name)[0] == pid


def test_a_new_and_an_old_project_live_side_by_side(backend):
    # The state this batch exists to prove: one machine, two layouts, no migration.
    old = old_style_project(backend)
    new = backend.create_project("A new book", "en", "zh", {})
    assert backend.project_dir(PROJECT) == old
    assert backend.project_dir(new).parent != old.parent
    # Listing both is what the interface does first, and it must not raise.
    assert {row["id"] for row in backend.list_projects()} == {PROJECT, new}


def test_a_project_chosen_location_is_recorded_and_still_found(tmp_path):
    workspace = tmp_path / "Wenyi Desktop"
    backend = LocalBackend(workspace)
    chosen = tmp_path / "Elsewhere"
    try:
        pid = backend.create_project("A book", "en", "zh", {}, library_root=str(chosen))
        assert backend.project_dir(pid).parent == chosen
        assert backend._get("projects", pid)["library_root"] == str(chosen)
    finally:
        backend.close()


def test_a_project_found_after_a_restart_that_names_no_default(tmp_path):
    # The next launch may be started without a library root -- an older parent, a
    # development run, a build that never recorded an installation. A project in a
    # folder the person chose still has to be reachable, or it is simply gone from
    # the list while its files sit on disk.
    workspace = tmp_path / "Wenyi Desktop"
    chosen = tmp_path / "Elsewhere"
    first = LocalBackend(workspace)
    try:
        pid = first.create_project("A book", "en", "zh", {}, library_root=str(chosen))
        first.storage_for(pid).close()
    finally:
        first.close()
    second = LocalBackend(workspace)
    try:
        assert second.project_dir(pid).parent == chosen
        assert second.get_project(pid)["available"] is True
    finally:
        second.close()


def test_deleting_a_project_in_a_chosen_library_removes_it(backend, roots):
    _, library = roots
    pid = backend.create_project("A book", "en", "zh", {})
    backend.storage_for(pid).close()
    directory = backend.project_dir(pid)
    assert directory.is_dir()
    backend.remove_project(pid)
    assert not directory.exists()
    assert backend.get_project(pid) is None


def test_deleting_an_old_project_still_removes_it(backend):
    directory = old_style_project(backend)
    backend.storage_for(PROJECT).close()
    backend.remove_project(PROJECT)
    assert not directory.exists()
    assert backend.get_project(PROJECT) is None


def test_the_marker_of_a_new_project_describes_its_own_directory(backend, roots):
    _, library = roots
    pid = backend.create_project("A book", "en", "zh", {})
    backend.storage_for(pid).close()
    directory = backend.project_dir(pid)
    marker = directory / paths.PROJECT_MARKER
    document = json.loads(marker.read_text(encoding="utf-8"))
    assert document["pid"] == pid
    assert document["library_root"] == str(directory.parent)
    # The workspace copy never existed; a marker filed there would be a second
    # directory claiming to be this project.
    assert not paths.project_marker(backend.workspace, pid).exists()


@pytest.mark.parametrize(
    "victim",
    ["workspace", "workspace-inner", "library", "runtime", "user", "profile", "image", "parent"],
)
def test_a_location_the_layout_owns_is_refused(backend, roots, tmp_path, monkeypatch, victim):
    workspace, library = roots
    profile = paths.platform_home()
    candidates = {
        "workspace": workspace,
        "workspace-inner": workspace / "data",
        "library": library,
        # Choosing exactly the default library is refused: that is the default,
        # not a choice, and every project would land in one undifferentiated pile.
        "runtime": paths.runtime_root(workspace) / "cache",
        "user": workspace,
        "profile": profile,
        "image": Path(tmp_path.anchor) / "Windows",
        "parent": workspace.parent,
    }
    with pytest.raises(LibraryLocationRefused):
        admit(candidates[victim], backend)


def test_a_location_elsewhere_is_admitted(backend, tmp_path):
    chosen = tmp_path / "Books" / "Wenyi"
    assert admit(chosen, backend) == chosen


@pytest.mark.parametrize(
    ("relative", "reason"),
    [
        ("program", "project_location_install"),
        ("program/projects", "project_location_install"),
        ("data", "project_location_workspace"),
        ("data/runtime", "project_location_workspace"),
        ("data/projects", "project_location_workspace"),
        ("chosen-books", None),
    ],
)
def test_project_locations_respect_the_actual_program_and_data_roots(relative, reason):
    # Use synthetic paths, not macOS's system-owned temporary directory.
    base = Path(Path.cwd().anchor) / "wenyi-library-path-test"
    backend = SimpleNamespace(
        workspace=base / "data", library_root=None, install_root=base / "program"
    )
    candidate = base / relative
    if reason is None:
        assert admit(candidate, backend) == candidate
    else:
        with pytest.raises(LibraryLocationRefused) as error:
            admit(candidate, backend)
        assert error.value.reason == reason


@pytest.mark.skipif(os.name == "nt", reason="POSIX system directories")
@pytest.mark.parametrize("directory", ["/var/tmp", "/var/folders"])
def test_project_locations_cannot_use_system_temporary_directories(backend, directory):
    with pytest.raises(LibraryLocationRefused) as error:
        admit(Path(directory) / "chosen-books", backend)
    assert error.value.reason == "project_location_system"


def test_a_relative_or_empty_location_is_refused(backend):
    for value in ("", "   ", "relative/folder", "."):
        with pytest.raises(LibraryLocationRefused):
            admit(value, backend)


def test_names_that_only_share_a_prefix_are_not_inside_each_other(backend, roots):
    workspace, _ = roots
    sibling = workspace.with_name(workspace.name + "2")
    # D:/Apps and D:/Apps2 share twelve characters and neither contains the other.
    assert admit(sibling, backend) == sibling


def test_a_refused_location_leaves_no_record_and_no_directory(backend, roots, tmp_path):
    workspace, library = roots
    with pytest.raises(LibraryLocationRefused):
        backend.create_project("A book", "en", "zh", {}, library_root=str(workspace))
    assert not library.exists()
    assert backend.list_projects() == []


def test_the_route_refuses_a_location_before_the_upload_is_written(tmp_path):
    context = create_context(tmp_path / "desktop")
    try:
        with TestClient(create_app(context=context)) as client:
            answer = client.post("/desktop/library/admit", json={"path": str(tmp_path / "desktop")})
            assert answer.status_code == 422
            assert answer.json()["code"] == "project_location_refused"
            assert answer.json()["message"]
            accepted = client.post("/desktop/library/admit", json={"path": str(tmp_path / "Books")})
            assert accepted.status_code == 200
            assert accepted.json()["path"] == str(tmp_path / "Books")
    finally:
        context.repository.close()


def test_a_project_created_through_the_route_lands_in_the_chosen_folder(tmp_path):
    context = create_context(tmp_path / "desktop")
    chosen = tmp_path / "Books"
    try:
        with use_context(context), TestClient(create_app(context=context)) as client:
            body = {
                "name": "A book",
                "source_lang": "en",
                "target_lang": "zh",
                "location": str(chosen),
            }
            created = client.post(
                "/projects",
                data={"project": json.dumps(body)},
                files={"file": ("book.txt", "Hello world.".encode(), "text/plain")},
            )
            assert created.status_code == 201, created.text
            pid = created.json()["id"]
            directory = context.repository.project_dir(pid)
            assert directory.parent == chosen
            # The source was written into the directory the record names, so the
            # upload and the record cannot disagree about where the project is.
            assert list(directory.glob("source-*.txt"))
            assert Path(context.data_dir) not in directory.parents
    finally:
        context.repository.close()
