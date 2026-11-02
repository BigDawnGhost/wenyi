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
import re
import sys
from pathlib import Path

from wenyi_core.pipeline.runstore import slugify

#: Catalog database of the old layout, holding both project records and the
#: credential tables.
_CATALOG = "local.sqlite3"
#: One directory per project inside the workspace.
_PROJECTS = "projects"
#: App-owned plugin installations.
_PLUGINS = "plugins"
#: The only plugin that owns a directory below `_PLUGINS`.
_BABELDOC = "babeldoc"
#: Saved plugin state; its presence is what makes a root an installation.
_PLUGIN_STATE = "plugin.json"
#: Completed plugin installations live below this directory.
_PLUGIN_INSTALLED = "installed"
#: Diagnostic log files written by the native side.
_DIAGNOSTICS = "diagnostics"
#: Workspace process lock; the native parent probes the same name.
_INSTANCE_LOCK = ".desktop.lock"
#: Native single-instance lock in the same directory.
_NATIVE_INSTANCE_LOCK = ".desktop-instance.lock"

#: Layer two root under the user profile.
_USER = "Wenyi"
#: Layer one root under the user profile.
_RUNTIME = ".wenyi"
#: Layer one entry for this application inside RUNTIME.
_RUNTIME_ENTRY = "desktop"
#: Environment override for layer one, matching `RUNTIME_OVERRIDE` in src/paths.rs.
_RUNTIME_OVERRIDE = "WENYI_RUNTIME_DIR"
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
#: Settings key remembering the library root a person used last.
#:
#: The recovery scan already reads this key when it has no catalog, so it is
#: written back whenever a project is created outside the default root. It holds
#: one root because a scan starts somewhere; the full set is `LIBRARY_ROOTS`.
LAST_LIBRARY_ROOT = "last_library_root"
#: Settings key holding every library root this installation has written under.
#:
#: A project's directory is found by looking for its identifier below one of
#: these roots, so the set has to outlive the catalog: losing it would make every
#: project created in a chosen folder unreachable, which is exactly the failure
#: this list exists to prevent. Order is the order they are searched in.
LIBRARY_ROOTS = "library_roots"

#: A project identifier is the first 16 characters of a version 4 UUID.
#:
#: Exactly one pattern describes it. Two call sites used to disagree -- the
#: catalog accepted any run of word characters while the recovery scan demanded
#: 16 hex digits -- and the disagreement was invisible until the scan found
#: nothing to recover. A project id is never generated any other way, so the
#: narrow pattern is the true one.
_PROJECT_ID = re.compile(r"[0-9a-f]{16}")
#: Length of a project id, and therefore where the readable suffix starts.
_PROJECT_ID_LENGTH = 16
#: Separator between the identifier and the readable suffix.
_PROJECT_SEPARATOR = "-"
#: Longest readable suffix kept in a directory name.
#:
#: A title has no length limit, and the directory name becomes the first of many
#: path components (chapters, source, exports) below it. Windows still refuses
#: paths past 260 characters unless long paths are enabled, so an unbounded title
#: turns into a project that cannot be written to. The identifier already makes
#: the name unique; the suffix only has to stay readable.
_PROJECT_SLUG_LIMIT = 64
#: Self-describing marker inside every project directory. Public because the
#: recovery scan reads it out of a directory it found without a catalog.
PROJECT_MARKER = "project.json"
#: Schema of that marker.
PROJECT_SCHEMA_VERSION = 1


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
    """One project directory of the layout that predates layer three.

    The caller owns the identifier's own validation; this only joins, so that a
    project path can never be built from a different directory than the one
    `projects()` names.

    This is not the general answer any more. A project may live in any admitted
    library root, so `LocalBackend.project_dir()` is the call to make; this
    function keeps naming the workspace layout, which is where every project
    written before layer three still is.
    """
    return projects(workspace) / pid


def project_in(root: Path, pid: str, name: str) -> Path:
    """One project directory below a library root, named after this project.

    Layer three has no fixed parent per installation: the root is whatever the
    person chose, so the project is named rather than numbered. The identifier
    leads the name so the directory still says which project it is once it has
    been moved, copied, or found by a scan with no catalog to consult.
    """
    return root / project_name(pid, name)


def find_project(root: Path, pid: str) -> Path | None:
    """Locate one project below one root, or `None` when it is not there.

    Both layouts are recognised, and neither is read out of a record. A project
    written before layer three is named by the identifier alone; the layer-three
    name is the identifier followed by the separator and a readable suffix. The
    identifier is fixed width, so a prefix test decides which of the two a
    directory is without guessing at the suffix.

    Scanning is what keeps a path free of catalog data: the roots come from
    configuration and the name comes from the identifier, so a record edited by
    hand can at worst point at a directory that is not this project's -- which
    the caller then refuses to use, rather than one it follows.
    """
    plain = root / pid
    if plain.is_dir():
        return plain
    try:
        entries = list(root.iterdir())
    except OSError:
        return None
    for entry in entries:
        # Both spellings begin with the identifier; `split_project_name` has
        # already answered "is this a project directory at all" for the scan.
        if split_project_name(entry.name)[0] == pid and entry.is_dir():
            return entry
    return None


def project_id(value: str) -> str:
    """The identifier a project directory name carries, or `""` when it carries none.

    One pattern answers for every caller that has to recognise a project: the
    deletion gate, the recovery scan, and the marker reader. They used to spell
    two different patterns, and the wider one accepted names the narrower one
    then skipped in silence.
    """
    return value if _PROJECT_ID.fullmatch(value) else ""


def _slug(value: str) -> str:
    """A bounded, filesystem-safe suffix for a project directory name."""
    return slugify(value)[:_PROJECT_SLUG_LIMIT]


def project_name(pid: str, slug: str) -> str:
    """The directory name of one project: its identifier, then a readable suffix.

    The identifier comes first and is fixed width, so the readable half can
    contain the separator itself. Titles do, because `slugify()` keeps hyphens;
    a name split on the separator would therefore find the wrong boundary, which
    is why `split_project_name()` counts characters instead.

    The suffix is truncated rather than rejected. A title with no length limit
    would otherwise decide how deep the paths below it may go, and the directory
    is only the first component of the paths to a project's chapters and exports.
    The identifier already makes the directory unique, so truncation costs
    readability and nothing else.
    """
    return f"{pid}{_PROJECT_SEPARATOR}{_slug(slug)}"


def split_project_name(name: str) -> tuple[str, str]:
    """Split a project directory name into its identifier and its suffix.

    Answers `("", "")` for a name that does not start with an identifier, so a
    caller never has to build a project path out of a directory it does not own.

    The boundary is the identifier's fixed length, not a search for the
    separator: a title may contain hyphens, and searching would cut at the first
    one. Both halves are re-validated here even though `project_name()` wrote
    them, because a directory can be renamed by hand.
    """
    head = name[:_PROJECT_ID_LENGTH]
    pid = project_id(head)
    if not pid:
        return "", ""
    if len(name) == _PROJECT_ID_LENGTH:
        return pid, ""
    if name[_PROJECT_ID_LENGTH] != _PROJECT_SEPARATOR:
        return "", ""
    return pid, name[_PROJECT_ID_LENGTH + 1 :]


def project_marker(workspace: Path, pid: str) -> Path:
    """The self-describing marker of a project in the old workspace layout.

    A directory can be moved, imported by hand, or found by a scan that has no
    catalog to consult. The marker is what carries the identifier across those
    moves, and it is the authority the catalog's own record is checked against.

    The marker describes a *directory*, so a caller that already resolved one
    uses `marker_in()` instead: naming it from the workspace would put the file
    in a directory the project does not live in once a library root is chosen.
    """
    return project(workspace, pid) / PROJECT_MARKER


def marker_in(directory: Path) -> Path:
    """The self-describing marker of a project directory that is already resolved."""
    return directory / PROJECT_MARKER


def plugins(workspace: Path, runtime: Path | None = None) -> Path:
    """The app-owned BabelDOC plugin root.

    Layer one is the application-owned root, but a workspace that already holds a
    working installation keeps using it: enabling the plugin after an upgrade
    must not hide the runtime and the state the user already has. Both roots are
    only inspected, never created or moved, so a downgrade back to the previous
    version keeps working.

    `runtime` is layer one and arrives from the native parent. When it is absent
    -- a standalone engine, or an older native parent -- the workspace copy is
    the only root there is, which is also what every call site used before layer
    one existed.
    """
    legacy = workspace / _PLUGINS / _BABELDOC
    if runtime is None:
        return legacy
    current = runtime / _PLUGINS / _BABELDOC
    if _holds_installation(current):
        return current
    if _holds_installation(legacy):
        return legacy
    return current


def _holds_installation(root: Path) -> bool:
    """Saved state or a completed installation is what makes a root authoritative."""
    return root.is_dir() and (
        (root / _PLUGIN_STATE).is_file() or (root / _PLUGIN_INSTALLED).is_dir()
    )


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


def runtime_root(*, home: Path | None = None) -> Path:
    """Layer one: application runtime that may be discarded and rebuilt.

    Mirrors `runtime_root()` in `src/paths.rs`, including the override, so both
    halves of the application name the same directory. Only the root itself is
    named here; nothing in this module creates or writes it.

    `WENYI_RUNTIME_DIR` is honoured because the native parent already honours it.
    A build that resolved a different layer one would leave the engine writing one
    cache while the parent cleared another.
    """
    configured = Path(os.environ.get(_RUNTIME_OVERRIDE, "").strip())
    if str(configured) and configured.is_absolute():
        return configured
    return (platform_home() if home is None else Path(home)) / _RUNTIME / _RUNTIME_ENTRY


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


# Resolution never checks whether the profile can be written to. A user-level
# directory that exists but refuses a write is a real condition this code cannot
# repair by choosing another one, and the failure belongs at the write, where the
# error can name the directory that refused, not here.


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
