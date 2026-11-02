"""The single place where Wenyi's own directories and files are named.

Every directory and file the engine derives is spelled here instead of at its
call site. The layer a root belongs to is part of the name:

| layer | root                            | lifetime                        |
|-------|---------------------------------|---------------------------------|
| one   | ~/.wenyi/desktop                | rebuilt with the application    |
| two   | ~/Wenyi                         | backed up, shared by projects   |
| three | a sibling of the installation   | the user's documents            |

The entries below one workspace are resolved from the directory the native
parent hands the engine as `--data-dir`. Layer two is resolved from the user
profile instead, which is the base `user_root()` uses in `src/paths.rs`, so both
halves of the application name the same directory. Layer two is not overridable
by an environment variable on purpose: a variable that moved it would silently
orphan every project the user already has.

Resolution is pure: nothing here creates, canonicalizes, or checks a directory
on disk, and nothing here decides permissions. Every function is exactly the
join its call site needs. That is what lets the workspace entries keep returning
what they returned before the user-level layer existed: `catalog()` still names
the file `LocalBackend` has always opened, and `projects()` still names the
directory `remove_project()` compares a resolved path against.

The engine opens the layer-two catalog now, and the workspace copy stays where
the old version wrote it. Both names have to keep working for as long as the two
layouts exist side by side: the migration reads `catalog()` and writes
`user_catalog()`, and no batch deletes either until the user asks for the
cleanup.

Three names are also spelled by code that does not import this module, and the
tests pin them so that neither side can be renamed alone:

- `.desktop.lock` is opened by `start()` in `src/instance.rs` before a
  workspace is admitted, independently of the engine process.
- `.desktop-instance.lock` is the native single-instance lock in the same
  directory; the migration probes it before reading a workspace it does not own.
- `local.sqlite3` is the catalog the native snapshot treats as one ordinary
  file of the workspace it copies. The native side deliberately never opens
  SQLite, so its Rust spellings are snapshot fixtures rather than a second
  resolution path; the name still has to move on both sides in the same batch,
  which is what the tests in `test_paths.py` are for.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: Catalog database of the old layout, holding both project records and the
#: credential tables.
_CATALOG = "local.sqlite3"
#: One directory per project inside the workspace.
_PROJECTS = "projects"
#: App-owned plugin installations.
_PLUGINS = "plugins"
#: The only plugin that owns a directory below `_PLUGINS`.
_BABELDOC = "babeldoc"
#: Diagnostic log files written by the native side.
_DIAGNOSTICS = "diagnostics"
#: Workspace process lock; the native parent probes the same name.
_INSTANCE_LOCK = ".desktop.lock"
#: Native single-instance lock in the same directory.
_NATIVE_INSTANCE_LOCK = ".desktop-instance.lock"

#: Layer two root under the user profile.
_USER = "Wenyi"
#: Settings document of layer two.
_SETTINGS = "settings.yaml"
#: Layer two directory holding state that every project shares.
_STORAGES = "storages"
#: Diagnostic logs of layer two.
_LOGS = "logs"
#: The catalog inside layer two. Deliberately a different file name from
#: `_CATALOG`: while both layouts exist, two identical names would make it
#: impossible to tell which one a running process has open.
_USER_CATALOG = "catalog.sqlite3"
#: Human-readable project index inside layer two.
_WORKSPACE_INDEX = "workspace.json"
#: Serializes the one-time move into layer two.
_MIGRATION_LOCK = ".migration.lock"


def catalog(workspace: Path) -> Path:
    """The catalog database of the old layout, at the workspace root.

    One SQLite file holds both the project catalog and the credential tables, so
    this single name decides where an existing workspace's data is for both. The
    engine opens `user_catalog()` instead; this function keeps the old location
    reachable, because the migration reads it and no batch deletes it.

    The native snapshot walker in `src/migration/backup.rs` copies this file as
    one ordinary member of the workspace, which is why a rename here that does
    not reach the snapshot in the same batch would only show up as missing data
    after an upgrade.
    """
    return workspace / _CATALOG


def projects(workspace: Path) -> Path:
    """The directory that holds one subdirectory per project."""
    return workspace / _PROJECTS


def project(workspace: Path, pid: str) -> Path:
    """One project's directory.

    The caller owns the identifier's own validation; this only joins, so that a
    project path can never be built from a different directory than the one
    `projects()` names.
    """
    return projects(workspace) / pid


def plugins(workspace: Path, runtime: Path | None = None) -> Path:
    """The app-owned BabelDOC plugin root.

    `runtime` is layer one. The parameter is carried so that the batch which
    moves plugins out of the workspace changes this function and no call site;
    until then it is ignored, and the workspace copy the engine has always used
    is returned unchanged.
    """
    del runtime
    return workspace / _PLUGINS / _BABELDOC


def diagnostics(workspace: Path) -> Path:
    """The workspace copy of the diagnostic logs.

    The native side writes the log files, and keeps a second root of its own for
    the case this one cannot be written to; see `log_roots` in `src/paths.rs`.
    This function names only the workspace entry, which is the first of that
    ordered list; no engine call site reads a log directory yet.
    """
    return workspace / _DIAGNOSTICS


def instance_lock(workspace: Path) -> Path:
    """The workspace process lock, deliberately at the workspace root.

    `start()` in `src/instance.rs` probes this same file before it admits a
    workspace, so the engine and its native parent have to agree on the name even
    though neither imports the other.
    """
    return workspace / _INSTANCE_LOCK


def native_instance_lock(workspace: Path) -> Path:
    """The native single-instance lock of one workspace.

    The engine never takes this lock; a migration only probes it, to refuse
    reading a workspace whose owning instance is still running. The name is
    spelled again in `src/instance.rs`, which owns the lock's lifetime.
    """
    return workspace / _NATIVE_INSTANCE_LOCK


def platform_home() -> Path:
    """The user profile that owns layers one and two.

    Resolved the way the native side resolves it, so the two halves of the
    application agree on the directory without passing it between them: the
    profile variable first, then the runtime's own answer. A blank or relative
    value counts as unset, because a variable set to nothing must never make
    layer two resolve against whatever directory the process happens to be in.

    Raises rather than inventing a location when the platform cannot name the
    profile at all. A user-level directory is the wrong thing to guess: guessing
    it wrong writes the user's settings and project index where nobody will look
    for them again.
    """
    name = "USERPROFILE" if sys.platform == "win32" else "HOME"
    configured = Path(os.environ.get(name, "").strip())
    if str(configured) and configured.is_absolute():
        return configured
    fallback = Path.home()
    if fallback.is_absolute():
        return fallback
    raise RuntimeError(
        "Wenyi could not determine your user folder, so it cannot create its "
        "settings directory. Check the user profile environment and reopen Wenyi."
    )


def user_root(*, home: Path | None = None) -> Path:
    """Layer two: user-level data that outlives the application.

    `home` is the profile to resolve against and defaults to this machine's.
    It is a parameter rather than an environment variable because layer two is
    persisted configuration: moving it is a user-visible action, not something a
    variable in the environment should be able to do by accident.
    """
    return (platform_home() if home is None else Path(home)) / _USER


def user_storages(*, home: Path | None = None) -> Path:
    """The layer-two directory that holds state every project shares."""
    return user_root(home=home) / _STORAGES


def user_catalog(*, home: Path | None = None) -> Path:
    """The catalog database the engine opens after the layer-two move.

    One SQLite file holds the project tables and the credential tables, the same
    two sets the workspace catalog held. Both stores have to resolve this one
    function, or one of them opens an empty database and the user sees either an
    empty project list or signed-out providers.
    """
    return user_storages(home=home) / _USER_CATALOG


def user_settings(*, home: Path | None = None) -> Path:
    """The settings document of layer two."""
    return user_root(home=home) / _SETTINGS


def user_workspace_index(*, home: Path | None = None) -> Path:
    """The human-readable project index of layer two."""
    return user_storages(home=home) / _WORKSPACE_INDEX


def user_logs(*, home: Path | None = None) -> Path:
    """The diagnostic log directory of layer two."""
    return user_root(home=home) / _LOGS


def user_migration_lock(*, home: Path | None = None) -> Path:
    """The lock that serializes the move into layer two.

    It lives inside the directory it protects, so it travels with the data and a
    second instance that finds it held knows which directory is being built.
    """
    return user_storages(home=home) / _MIGRATION_LOCK
