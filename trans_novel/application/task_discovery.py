"""Read-only discovery of legacy tasks by their durable source identity.

Discovery belongs before backend construction: callers must be able to locate an
existing :class:`RunStore` directory without importing or instantiating that
legacy adapter.  This module therefore depends only on the standard library and
never creates, repairs, or migrates state.
"""

from __future__ import annotations

import json
import re
import stat
from pathlib import Path
from typing import Any

_LEGACY_MARKER_NAMES = ("manifest.json", ".initializing.json")
_SHA256_PATTERN = re.compile(r"\A[0-9a-f]{64}\Z")


class LegacyTaskDiscoveryError(RuntimeError):
    """Base error for legacy state that cannot be inspected safely."""


class InvalidLegacyTaskMarker(LegacyTaskDiscoveryError):
    """Raised when a legacy owner marker is unreadable or malformed."""


class LegacyTaskDiscoveryConflict(LegacyTaskDiscoveryError):
    """Raised when durable legacy identities do not identify one directory."""


def discover_legacy_run_dir(state_root: Path, source_sha256: str) -> Path | None:
    """Find the one direct legacy run directory owned by ``source_sha256``.

    Only ``manifest.json`` and ``.initializing.json`` are owner evidence.  Other
    directories and files under ``state_root`` are ignored, and nested paths are
    never searched.  Once either marker name exists, however, its contents must
    be valid even when it does not match the requested digest.  Failing closed in
    that case prevents corrupt legacy state from being adopted by a new backend.

    Args:
        state_root: Parent directory containing per-task state directories.
        source_sha256: Canonical digest already computed by the admission layer.

    Returns:
        The matching direct child directory, or ``None`` when no legacy task owns
        the digest.  The function never creates ``state_root``.

    Raises:
        ValueError: ``source_sha256`` is not 64 lowercase hexadecimal characters.
        InvalidLegacyTaskMarker: A legacy marker cannot be trusted.
        LegacyTaskDiscoveryConflict: Markers disagree or multiple runs claim the
            requested digest.
        LegacyTaskDiscoveryError: The root or a candidate directory cannot be
            inspected safely.
    """
    requested_digest = _validate_source_sha256(source_sha256)
    root_status = _directory_status(state_root)
    if root_status == "missing":
        return None
    if root_status == "other":
        raise LegacyTaskDiscoveryError(f"legacy state root is not a directory: {state_root}")

    # Materialize the direct-child snapshot once.  This keeps the traversal
    # boundary explicit and lets an iteration error fail closed before matching.
    try:
        entries = tuple(state_root.iterdir())
    except OSError as error:
        raise LegacyTaskDiscoveryError(f"cannot inspect legacy state root: {state_root}") from error

    matches: list[Path] = []
    for entry in entries:
        if not _is_direct_directory(entry):
            continue
        owner_digest = _read_legacy_owner(entry)
        if owner_digest == requested_digest:
            matches.append(entry)

    # More than one owner cannot be resolved by ordering or slug preference:
    # choosing either path could resume and mutate the wrong legacy task.
    if len(matches) > 1:
        rendered = ", ".join(str(path) for path in sorted(matches))
        raise LegacyTaskDiscoveryConflict(
            f"multiple legacy run directories claim source_sha256 {requested_digest}: {rendered}"
        )
    return matches[0] if matches else None


def _validate_source_sha256(value: object) -> str:
    """Accept only the canonical digest form written by legacy RunStore."""
    if type(value) is not str or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError("source_sha256 must be 64 lowercase hexadecimal characters")
    return value


def _directory_status(path: Path) -> str:
    """Classify a root path without treating a dangling link as missing state."""
    try:
        path_status = path.stat()
    except FileNotFoundError:
        try:
            path.lstat()
        except FileNotFoundError:
            return "missing"
        except OSError as error:
            raise LegacyTaskDiscoveryError(f"cannot inspect legacy state root: {path}") from error
        return "other"
    except OSError as error:
        raise LegacyTaskDiscoveryError(f"cannot inspect legacy state root: {path}") from error
    return "directory" if stat.S_ISDIR(path_status.st_mode) else "other"


def _is_direct_directory(path: Path) -> bool:
    """Recognize physical child directories while refusing to follow child links."""
    try:
        path_status = path.lstat()
    except OSError as error:
        raise LegacyTaskDiscoveryError(f"cannot inspect legacy state entry: {path}") from error

    # A linked directory may contain the legacy owner being searched for.  It
    # must not look like an unrelated file merely because discovery refuses to
    # traverse it.  Windows junctions are reparse points even when ``S_ISLNK``
    # is false, so inspect the platform file attributes when they are present.
    file_attributes = getattr(path_status, "st_file_attributes", 0)
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if stat.S_ISLNK(path_status.st_mode) or (
        reparse_attribute and file_attributes & reparse_attribute
    ):
        raise LegacyTaskDiscoveryError(
            f"legacy state entry must not be a link or reparse point: {path}"
        )
    return stat.S_ISDIR(path_status.st_mode)


def _read_legacy_owner(run_dir: Path) -> str | None:
    """Return one validated owner shared by all markers in ``run_dir``."""
    owners: dict[str, str] = {}
    for marker_name in _LEGACY_MARKER_NAMES:
        marker_path = run_dir / marker_name
        if _marker_exists(marker_path):
            owners[marker_name] = _read_marker_digest(marker_path)

    if not owners:
        return None
    if len(set(owners.values())) != 1:
        rendered = ", ".join(f"{name}={digest}" for name, digest in owners.items())
        raise LegacyTaskDiscoveryConflict(f"legacy owner markers disagree in {run_dir}: {rendered}")
    return next(iter(owners.values()))


def _marker_exists(path: Path) -> bool:
    """Check marker presence and reject links, directories, and special nodes."""
    try:
        marker_status = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise InvalidLegacyTaskMarker(f"cannot inspect legacy owner marker: {path}") from error
    if not stat.S_ISREG(marker_status.st_mode):
        raise InvalidLegacyTaskMarker(f"legacy owner marker is not a regular file: {path}")
    return True


def _read_marker_digest(path: Path) -> str:
    """Decode a marker object and extract its canonical source identity."""
    try:
        with path.open("r", encoding="utf-8") as marker_file:
            marker = json.load(marker_file, object_pairs_hook=_unique_json_object)
    except (OSError, UnicodeError, json.JSONDecodeError, _DuplicateJsonKey) as error:
        raise InvalidLegacyTaskMarker(f"invalid legacy owner marker: {path}") from error

    if type(marker) is not dict:
        raise InvalidLegacyTaskMarker(f"legacy owner marker must contain a JSON object: {path}")
    try:
        return _validate_source_sha256(marker.get("source_sha256"))
    except ValueError as error:
        raise InvalidLegacyTaskMarker(
            f"legacy owner marker has invalid source_sha256: {path}"
        ) from error


class _DuplicateJsonKey(ValueError):
    """Internal signal used to reject ambiguous JSON objects."""


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build a JSON object while rejecting duplicate keys at every depth."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


__all__ = [
    "InvalidLegacyTaskMarker",
    "LegacyTaskDiscoveryConflict",
    "LegacyTaskDiscoveryError",
    "discover_legacy_run_dir",
]
