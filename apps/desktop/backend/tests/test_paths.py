"""The engine's half of the workspace layout, pinned against the other half.

Every function here is a pure join, so the tests are small. Their job is not to
describe what `pathlib` does: it is to fail when one half of the layout is
renamed and the other is not. Two of the names below are also spelled by the
native parent, which neither imports this module nor runs the engine, so each of
those is asserted twice — once as this module's answer, and once against the
Rust source that spells it.
"""

from __future__ import annotations

from pathlib import Path

from wenyi_desktop import paths
from wenyi_desktop.local_backend import LocalBackend
from wenyi_desktop.local_credentials import CredentialStore
from wenyi_desktop.local_runtime import WorkspaceLock

# apps/desktop/backend/tests/test_paths.py -> apps/desktop
DESKTOP = Path(__file__).resolve().parents[2]
PROJECT = "0123456789abcdef"


def rust(relative: str) -> str:
    return (DESKTOP / relative).read_text(encoding="utf-8")


def test_catalog_is_the_file_the_engine_opens(tmp_path: Path) -> None:
    workspace = tmp_path / "Wenyi Desktop"
    assert paths.catalog(workspace) == workspace / "local.sqlite3"


def test_the_catalog_and_the_credentials_share_one_file(tmp_path: Path) -> None:
    workspace = tmp_path / "Wenyi Desktop"
    workspace.mkdir()
    # One SQLite file holds both stores. If the two ever diverge, one of them
    # opens an empty database, and the user sees an empty project list or
    # signed-out model providers.
    assert CredentialStore(workspace).database == paths.catalog(workspace)
    backend = LocalBackend(workspace)
    try:
        assert backend.database == paths.catalog(workspace)
    finally:
        backend.close()


def test_projects_holds_one_directory_per_project(tmp_path: Path) -> None:
    workspace = tmp_path / "Wenyi Desktop"
    assert paths.projects(workspace) == workspace / "projects"


def test_a_project_directory_is_below_the_projects_directory(tmp_path: Path) -> None:
    workspace = tmp_path / "Wenyi Desktop"
    assert paths.project(workspace, PROJECT) == workspace / "projects" / PROJECT
    # One join, so a project path can never come from another directory.
    assert paths.project(workspace, PROJECT).parent == paths.projects(workspace)


def test_the_project_directory_is_the_one_deletion_admits(tmp_path: Path) -> None:
    workspace = tmp_path / "Wenyi Desktop"
    backend = LocalBackend(workspace)
    try:
        # remove_project() refuses a directory whose resolved parent is not
        # paths.projects(). That gate is only sound while these two agree.
        assert backend.project_dir(PROJECT) == paths.project(workspace, PROJECT)
    finally:
        backend.close()


def test_plugins_are_named_inside_the_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "Wenyi Desktop"
    runtime = tmp_path / "profile" / ".wenyi" / "desktop"
    # Layer one is carried but unused: plugins still live in the workspace, and
    # the batch that moves them changes this function rather than a call site.
    assert paths.plugins(workspace) == workspace / "plugins" / "babeldoc"
    assert paths.plugins(workspace, runtime) == workspace / "plugins" / "babeldoc"


def test_diagnostics_name_the_workspace_log_root(tmp_path: Path) -> None:
    workspace = tmp_path / "Wenyi Desktop"
    assert paths.diagnostics(workspace) == workspace / "diagnostics"


def test_the_lock_stays_at_the_workspace_root(tmp_path: Path) -> None:
    workspace = tmp_path / "Wenyi Desktop"
    # Reviewed and fixed: the workspace lock is not layer two, so it must not
    # follow the catalog into the user directory.
    assert paths.instance_lock(workspace) == workspace / ".desktop.lock"
    assert WorkspaceLock(workspace).path == paths.instance_lock(workspace)


def test_the_catalog_name_is_the_one_the_native_snapshot_copies() -> None:
    # The native snapshot copies a workspace byte for byte and names this file,
    # so a rename here alone would restore a workspace whose catalog is gone.
    assert paths.catalog(Path("C:/workspace")).name == "local.sqlite3"
    assert '"local.sqlite3"' in rust("src/migration/backup.rs")


def test_the_lock_name_is_the_one_the_native_parent_probes() -> None:
    # start() in the native parent probes this file before it admits a
    # workspace, and it never runs the engine, so a disagreement means the two
    # processes lock different files.
    assert paths.instance_lock(Path("C:/workspace")).name == ".desktop.lock"
    assert 'workspace.join(".desktop.lock")' in rust("src/instance.rs")


def test_the_diagnostics_name_is_the_one_the_native_resolver_builds() -> None:
    # log_roots() in the native resolver starts with workspace.join(DIAGNOSTICS)
    # for the logs it writes itself; this is that same directory.
    assert paths.diagnostics(Path("C:/workspace")).name == "diagnostics"
    assert 'const DIAGNOSTICS: &str = "diagnostics";' in rust("src/paths.rs")
