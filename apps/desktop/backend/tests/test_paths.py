"""The engine's half of the layout, pinned against the other half.

Every function here is a pure join, so the tests are small. Their job is not to
describe what `pathlib` does: it is to fail when one half of the layout is renamed
and the other is not. Several of the names below are also spelled by the native
parent, which neither imports this module nor runs the engine, so each of those
is asserted twice -- once as this module's answer, and once against the Rust
source that spells it.

The user-level names are the ones this batch moved, so they carry the extra
weight: the engine opens `user_catalog()` while the old workspace still holds
`catalog()`, and the two must never collapse into one name while both exist.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from wenyi_desktop import paths
from wenyi_desktop.local_backend import LocalBackend
from wenyi_desktop.local_credentials import CredentialStore
from wenyi_desktop.local_runtime import WorkspaceLock

# apps/desktop/backend/tests/test_paths.py -> apps/desktop
DESKTOP = Path(__file__).resolve().parents[2]
PROJECT = "0123456789abcdef"


def rust(relative: str) -> str:
    return (DESKTOP / relative).read_text(encoding="utf-8")


def test_the_old_catalog_is_still_named_in_the_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "Wenyi Desktop"
    # Kept on purpose: the migration reads this file and no batch deletes it.
    assert paths.catalog(workspace) == workspace / "local.sqlite3"


def test_the_engine_catalog_is_the_one_under_the_user_profile(tmp_path: Path) -> None:
    home = tmp_path / "profile"
    assert paths.user_catalog(home=home) == home / "Wenyi" / "storages" / "catalog.sqlite3"


def test_the_two_catalog_names_are_different_files(tmp_path: Path) -> None:
    workspace = tmp_path / "Wenyi Desktop"
    home = tmp_path / "profile"
    # While both layouts exist, an identical file name would make it impossible
    # to tell which database a running process has open.
    assert paths.catalog(workspace).name != paths.user_catalog(home=home).name


def test_the_catalog_and_the_credentials_share_one_file(tmp_path: Path) -> None:
    workspace = tmp_path / "Wenyi Desktop"
    workspace.mkdir()
    home = tmp_path / "profile"
    # One SQLite file holds both stores, as it did in the workspace. If the two
    # ever diverge, one of them opens an empty database, and the user sees an
    # empty project list or signed-out model providers.
    store = CredentialStore(workspace, user_home=home)
    assert store.database == paths.user_catalog(home=home)
    backend = LocalBackend(workspace, user_home=home)
    try:
        assert backend.database == paths.user_catalog(home=home)
        assert backend.database == store.database
    finally:
        backend.close()


def test_the_workspace_keeps_the_project_directories(tmp_path: Path) -> None:
    workspace = tmp_path / "Wenyi Desktop"
    backend = LocalBackend(workspace, user_home=tmp_path / "profile")
    try:
        # Only the catalog moved. Project directories and the locks stay with
        # the workspace until the batch that owns layer three moves them.
        assert backend.workspace == workspace
        assert backend.database.is_relative_to(paths.user_root(home=tmp_path / "profile"))
        assert backend.project_dir(PROJECT).is_relative_to(paths.projects(workspace))
    finally:
        backend.close()


def test_projects_holds_one_directory_per_project(tmp_path: Path) -> None:
    workspace = tmp_path / "Wenyi Desktop"
    assert paths.projects(workspace) == workspace / "projects"


@pytest.mark.parametrize(
    ("id", "expected"),
    [
        ("0123456789abcdef", "0123456789abcdef"),
        ("0123456789ABCDEF", ""),
        ("0123456789abcde", ""),
        ("0123456789abcdef0", ""),
        ("not-an-identifier", ""),
        ("", ""),
        ("../escape", ""),
    ],
)
def test_one_pattern_decides_what_a_project_identifier_is(id, expected) -> None:
    # The deletion gate, the recovery scan and the marker reader all ask this one
    # question. They used to answer it with two different patterns, and the wider
    # one admitted names the narrower one then skipped without a log line.
    assert paths.project_id(id) == expected


def test_a_project_directory_name_keeps_the_identifier_first() -> None:
    # design M-2: the identifier survives the readable suffix, so a directory
    # name alone is enough to rebuild the catalog entry it belongs to.
    assert paths.project_name(PROJECT, "my book") == f"{PROJECT}-my_book"
    assert paths.project_name(PROJECT, "第一章 / 开端") == f"{PROJECT}-第一章_开端"
    # An empty title still yields a usable directory rather than a bare id.
    assert paths.project_name(PROJECT, "  ") == f"{PROJECT}-book"


def test_a_title_with_hyphens_survives_the_round_trip() -> None:
    # slugify() keeps hyphens, so a name split on the separator would cut at the
    # first hyphen of the title and report a project id of "a1b2c3".
    name = paths.project_name(PROJECT, "a-b-c")
    assert name == f"{PROJECT}-a-b-c"
    assert paths.split_project_name(name) == (PROJECT, "a-b-c")


def test_a_long_title_is_truncated_rather_than_refused() -> None:
    # The directory name is only the first component of the paths to a project's
    # chapters and exports. An unbounded title makes those paths unusable rather
    # than merely ugly, and Windows refuses them outright.
    name = paths.project_name(PROJECT, "x" * 200)
    assert len(name) == len(PROJECT) + 1 + 64
    assert paths.split_project_name(name) == (PROJECT, "x" * 64)


def test_a_name_that_does_not_start_with_an_identifier_is_not_a_project() -> None:
    for name in ("a1b2c3d4e5f60718abc", "1.2", "projects", "", "a1b2c3d4e5f6071g-x"):
        assert paths.split_project_name(name) == ("", "")


def test_an_identifier_on_its_own_is_a_project_without_a_title() -> None:
    # Every directory the current layout writes is exactly this shape, so a
    # recovery scan has to read it as a project rather than reject it.
    assert paths.split_project_name(PROJECT) == (PROJECT, "")
    assert paths.project_id(PROJECT) == PROJECT


def test_the_marker_is_named_the_same_way_by_every_reader() -> None:
    # The recovery scan reads this file out of a directory it found without a
    # catalog, so the two sides cannot name it differently.
    workspace = Path("C:/workspace")
    assert paths.project_marker(workspace, PROJECT) == (
        workspace / "projects" / PROJECT / "project.json"
    )
    assert paths.project_marker(workspace, PROJECT).name == "project.json"


def test_a_project_directory_is_below_the_projects_directory(tmp_path: Path) -> None:
    workspace = tmp_path / "Wenyi Desktop"
    assert paths.project(workspace, PROJECT) == workspace / "projects" / PROJECT
    # One join, so a project path can never come from another directory.
    assert paths.project(workspace, PROJECT).parent == paths.projects(workspace)


def test_a_created_project_writes_its_own_marker(tmp_path: Path) -> None:
    # design §5.3 S5: the marker is what lets a scan rebuild a catalog entry it has
    # no record of, so it has to exist before anything else in the directory does.
    home = tmp_path / "profile"
    backend = LocalBackend(tmp_path / "Wenyi Desktop", user_home=home)
    try:
        pid = backend.create_project("A book", "en", "zh", {})
        backend.storage_for(pid).close()
        marker = paths.project_marker(backend.workspace, pid)
        document = json.loads(marker.read_text(encoding="utf-8"))
        assert document["pid"] == pid
        assert document["name"] == "A book"
        assert document["library_root"] == str(paths.projects(backend.workspace))
        assert document["schema_version"] == paths.PROJECT_SCHEMA_VERSION
    finally:
        backend.close()


def test_the_marker_of_a_missing_project_is_not_invented(tmp_path: Path) -> None:
    # Reading a project must never create the directory it names: a listing call
    # that wrote to disk would make every catalog read a mutation.
    home = tmp_path / "profile"
    backend = LocalBackend(tmp_path / "Wenyi Desktop", user_home=home)
    try:
        pid = backend.create_project("A book", "en", "zh", {})
        backend.storage_for(pid).close()
        marker = paths.project_marker(backend.workspace, pid)
        marker.unlink()
        backend.storage_for(pid, create=False).close()
        assert not marker.exists()
    finally:
        backend.close()


def test_the_project_directory_is_the_one_deletion_admits(tmp_path: Path) -> None:
    workspace = tmp_path / "Wenyi Desktop"
    backend = LocalBackend(workspace, user_home=tmp_path / "profile")
    try:
        # remove_project() refuses a directory whose resolved parent is not
        # paths.projects(). That gate is only sound while these two agree.
        assert backend.project_dir(PROJECT) == paths.project(workspace, PROJECT)
    finally:
        backend.close()


def test_plugins_are_named_inside_the_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "Wenyi Desktop"
    runtime = tmp_path / "profile" / ".wenyi" / "desktop"
    # With no layer one the workspace copy is the only root there is, which is
    # what a standalone engine and an older native parent both get.
    assert paths.plugins(workspace) == workspace / "plugins" / "babeldoc"
    # With layer one and nothing installed anywhere, the application-owned root
    # wins: new plugins belong there, not in the workspace.
    assert paths.plugins(workspace, runtime) == runtime / "plugins" / "babeldoc"


def test_an_installation_in_the_workspace_is_still_used(tmp_path: Path) -> None:
    workspace = tmp_path / "Wenyi Desktop"
    runtime = tmp_path / "profile" / ".wenyi" / "desktop"
    legacy = workspace / "plugins" / "babeldoc"
    legacy.mkdir(parents=True)
    (legacy / "plugin.json").write_text("{}", encoding="utf-8")
    # A workspace that already holds a working installation keeps using it: an
    # upgrade must not hide the runtime and the state the user already has.
    assert paths.plugins(workspace, runtime) == legacy


def test_a_completed_layer_one_installation_wins_over_the_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "Wenyi Desktop"
    runtime = tmp_path / "profile" / ".wenyi" / "desktop"
    legacy = workspace / "plugins" / "babeldoc"
    (legacy / "installed" / ("a" * 32)).mkdir(parents=True)
    current = runtime / "plugins" / "babeldoc"
    (current / "installed" / ("b" * 32)).mkdir(parents=True)
    # Both roots hold something; the application-owned one is the future.
    assert paths.plugins(workspace, runtime) == current


def test_diagnostics_name_the_workspace_log_root(tmp_path: Path) -> None:
    workspace = tmp_path / "Wenyi Desktop"
    assert paths.diagnostics(workspace) == workspace / "diagnostics"


def test_the_lock_stays_at_the_workspace_root(tmp_path: Path) -> None:
    workspace = tmp_path / "Wenyi Desktop"
    # Reviewed and fixed: the workspace lock is not layer two, so it must not
    # follow the catalog into the user directory.
    assert paths.instance_lock(workspace) == workspace / ".desktop.lock"
    assert WorkspaceLock(workspace).path == paths.instance_lock(workspace)


def test_the_native_instance_lock_is_named_beside_the_engine_lock(tmp_path: Path) -> None:
    workspace = tmp_path / "Wenyi Desktop"
    # The migration probes this file to refuse reading a workspace whose old
    # instance is still running. It is never taken over.
    assert paths.native_instance_lock(workspace) == workspace / ".desktop-instance.lock"
    assert paths.native_instance_lock(workspace).parent == paths.instance_lock(workspace).parent


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("user_root", ()),
        ("user_storages", ("storages",)),
        ("user_catalog", ("storages", "catalog.sqlite3")),
        ("user_settings", ("settings.yaml",)),
        ("user_workspace_index", ("storages", "workspace.json")),
        ("user_logs", ("logs",)),
        ("user_migration_lock", ("storages", ".migration.lock")),
    ],
)
def test_every_layer_two_entry_hangs_below_the_user_root(tmp_path: Path, name, expected) -> None:
    home = tmp_path / "profile"
    resolved = getattr(paths, name)(home=home)
    assert resolved == home.joinpath("Wenyi", *expected)
    assert resolved.is_relative_to(paths.user_root(home=home))


def test_a_variable_cannot_move_layer_two(tmp_path, monkeypatch, private_profile) -> None:
    # Layer two is persisted configuration, not an overridable root: a variable
    # that moved it would orphan every project the user already has. Only the
    # profile itself is read, and that is the value the fixture set.
    monkeypatch.setenv("WENYI_USER_DIR", str(tmp_path / "elsewhere"))
    assert paths.user_root() == private_profile / "Wenyi"


def test_a_blank_profile_variable_is_not_the_current_directory(
    tmp_path, monkeypatch, private_profile
) -> None:
    # A value set to nothing has to count as unset. Resolving it as-is would put
    # layer two in a relative directory, which every later start would then
    # resolve against a different working directory.
    monkeypatch.setenv("USERPROFILE", "   ")
    monkeypatch.setenv("HOME", "")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: private_profile))
    assert paths.platform_home() == private_profile
    assert paths.platform_home() != Path.cwd()


def test_a_profile_that_cannot_be_named_is_refused_rather_than_guessed(
    tmp_path, monkeypatch
) -> None:
    # The last resort is refusal, not the working directory: a guessed layer two
    # writes the user's settings and project index where nobody will find them.
    monkeypatch.setenv("USERPROFILE", "not-a-path")
    monkeypatch.setenv("HOME", "")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: cls("relative")))
    with pytest.raises(RuntimeError, match="user folder"):
        paths.platform_home()


def test_the_two_roots_the_design_names_do_not_overlap(tmp_path: Path) -> None:
    # design §3.3: layer one and layer two must be pairwise disjoint.
    layer_one = paths.platform_home() / ".wenyi"
    layer_two = paths.user_root()
    assert not layer_two.is_relative_to(layer_one)
    assert not layer_one.is_relative_to(layer_two)


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


def test_the_native_instance_lock_name_is_the_one_the_native_parent_holds() -> None:
    # The migration probes the lock the previous version holds. If the two sides
    # disagreed on the name, the probe would always report "not running" and the
    # migration would read a workspace an old instance is still writing.
    assert paths.native_instance_lock(Path("C:/workspace")).name == ".desktop-instance.lock"
    assert 'const LOCK: &str = ".desktop-instance.lock";' in rust("src/instance.rs")


def test_the_layer_two_root_is_the_one_the_native_resolver_builds() -> None:
    # user_root() in the native resolver is home_dir().join(USER). Layer two has
    # to be the same directory from both halves, because the native side writes
    # the diagnostic logs and the backup into it.
    assert paths.user_root(home=Path("C:/profile")) == Path("C:/profile/Wenyi")
    assert 'const USER: &str = "Wenyi";' in rust("src/paths.rs")


def test_the_diagnostics_name_is_the_one_the_native_resolver_builds() -> None:
    # log_roots() in the native resolver starts with workspace.join(DIAGNOSTICS)
    # for the logs it writes itself; this is that same directory.
    assert paths.diagnostics(Path("C:/workspace")).name == "diagnostics"
    assert 'const DIAGNOSTICS: &str = "diagnostics";' in rust("src/paths.rs")
