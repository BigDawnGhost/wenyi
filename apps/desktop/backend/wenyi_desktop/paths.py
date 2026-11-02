"""The single place where a Desktop workspace's own entries are named.

Every directory and file the engine derives from an admitted workspace is
spelled here instead of at its call site. The layer a root belongs to is part of
the name:

| layer | root                            | lifetime                        |
|-------|---------------------------------|---------------------------------|
| one   | ~/.wenyi/desktop                | rebuilt with the application    |
| two   | ~/Wenyi                         | backed up, shared by projects   |
| three | a sibling of the installation   | the user's documents            |

This module covers the entries that live *inside* one workspace. The three
layer roots themselves are resolved by the native parent in `src/paths.rs` and
reach the engine as `--data-dir` (the workspace) and `--runtime-dir` (layer
one); nothing here reads an environment variable or walks a platform directory.

Resolution is pure: nothing here creates, canonicalizes, or checks a directory
on disk, and nothing here decides permissions. Every function is exactly the
join the call sites used to spell by hand, which is what keeps this batch free
of behaviour changes: `catalog()` returns the very file `LocalBackend` already
opened, and `projects()` returns the very directory `remove_project()` already
compared a resolved path against.

Two names are also spelled by code that does not import this module, and the
tests pin them so that neither side can be renamed alone:

- `.desktop.lock` is opened by `start()` in `src/instance.rs` before a
  workspace is admitted, independently of the engine process.
- `local.sqlite3` is the catalog the native snapshot treats as one ordinary file
  of the workspace it copies. The native side deliberately never opens SQLite,
  so its Rust spellings are snapshot fixtures rather than a second resolution
  path; the name still has to move on both sides in the same batch, which is
  what the tests below are for.
"""

from __future__ import annotations

from pathlib import Path

#: Catalog database, holding both project records and the credential tables.
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


def catalog(workspace: Path) -> Path:
    """The catalog database, at the workspace root.

    One SQLite file holds both the project catalog and the credential tables, so
    this single name decides where an existing workspace's data is for both.

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
