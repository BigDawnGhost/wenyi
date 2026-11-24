"""The breaking Desktop layout has one root and never adopts legacy data."""

import sqlite3

import pytest
from fastapi.testclient import TestClient
from wenyi_desktop import paths
from wenyi_desktop.local_backend import LocalBackend
from wenyi_desktop.local_credentials import CredentialStore
from wenyi_desktop.main import create_app, create_context
from wenyi_desktop.settings import DesktopSettings


def test_catalog_credentials_and_projects_belong_to_the_explicit_root(tmp_path):
    root = tmp_path / "chosen"
    backend = LocalBackend(root)
    credentials = CredentialStore(root)
    try:
        assert backend.database == credentials.database == root / "catalog.sqlite3"
        assert paths.catalog(root) == backend.database
        pid = backend.create_project("Example", "en", "zh", {})
        assert backend.project_dir(pid).parent == root / "projects"
        assert paths.instance_lock(root).parent == root / "locks"
        assert paths.plugins(root).is_relative_to(root / "runtime")
        assert paths.diagnostics(root) == root / "logs"
    finally:
        backend.close()


def test_two_explicit_roots_do_not_share_settings_projects_or_credentials(tmp_path):
    first = create_context(tmp_path / "first")
    second = create_context(tmp_path / "second")
    first_backend = first.repository
    assert isinstance(first_backend, LocalBackend)
    second_backend = second.repository
    assert isinstance(second_backend, LocalBackend)
    first_settings = first.settings_store
    assert isinstance(first_settings, DesktopSettings)
    second_settings = second.settings_store
    assert isinstance(second_settings, DesktopSettings)
    try:
        first_backend.create_project("Only in first", "en", "zh", {})
        with first_backend.transaction() as connection:
            first_backend.save_settings({"revision": 7}, connection)
        assert second_backend.list_projects() == []
        assert second_backend.load_settings() is None
        assert first_settings.credentials.database != second_settings.credentials.database
    finally:
        first_backend.close()
        second_backend.close()


def test_startup_does_not_scan_or_modify_unregistered_project_directories(tmp_path):
    root = tmp_path / "fresh"
    unregistered = root / "projects" / ("a" * 16)
    unregistered.mkdir(parents=True)
    state = unregistered / "state.sqlite3"
    state.write_bytes(b"Unregistered data must not be opened, migrated or deleted.")
    legacy = root / "local.sqlite3"
    legacy.write_bytes(b"An old catalog must not be adopted.")
    before = {path: path.read_bytes() for path in (state, legacy)}
    with TestClient(create_app(root)) as client:
        assert client.get("/projects").json() == []
    assert {path: path.read_bytes() for path in before} == before


def test_default_project_library_does_not_follow_the_installation(tmp_path):
    root = tmp_path / "data"
    backend = LocalBackend(root, install_root=tmp_path / "Programs" / "Wenyi Desktop")
    try:
        assert backend.new_project_dir("a" * 16, "Example", None).parent == root / "projects"
    finally:
        backend.close()


@pytest.mark.parametrize("store", [LocalBackend, CredentialStore])
def test_renamed_old_catalog_is_refused_without_changing_it(tmp_path, store):
    database = tmp_path / "catalog.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE application_settings(document TEXT)")
        connection.execute("INSERT INTO application_settings VALUES ('old settings')")
    before = database.read_bytes()
    with pytest.raises(RuntimeError, match="current-format Desktop"):
        store(tmp_path)
    assert database.read_bytes() == before
