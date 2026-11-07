"""Admit custom libraries only outside the program, data root and system folders.

The actual data root participates, including a root selected with --data-dir.
The default projects child is internal; this policy is for externally chosen roots.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import paths

#: Windows environment variables that name directories the operating system owns.
_SYSTEM_VARIABLES = (
    "SystemRoot",
    "windir",
    "ProgramFiles",
    "ProgramFiles(x86)",
    "ProgramData",
)
#: Roots the POSIX platforms reserve for the system.
_SYSTEM_ROOTS = ("/usr", "/bin", "/sbin", "/etc", "/boot", "/var")


class LibraryLocationRefused(Exception):
    """A chosen location this application will not keep project data in.

    The sentence travels with the refusal because the interface shows it verbatim:
    "that folder is inside Wenyi's program folder" tells a person which folder to
    choose instead, and "invalid location" does not.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


def _comparable(path: Path) -> tuple[str, ...]:
    """A path as whole components, so containment is never a string prefix.

    `D:/Apps` and `D:/Apps2` share twelve characters and neither contains the
    other. Comparing components is what keeps that from being a silent pass.
    """
    text = os.path.abspath(str(path)).replace("\\", "/")
    if os.name != "nt":
        return tuple(part for part in text.split("/") if part)
    # Windows spellings are case-insensitive and may carry a verbatim prefix.
    text = text.lower()
    if text.startswith("//?/"):
        text = text[4:]
    return tuple(part for part in text.split("/") if part)


def _overlaps(left: Path, right: Path) -> bool:
    """Whether the two name the same directory or one contains the other."""
    first = _comparable(left)
    second = _comparable(right)
    if not first or not second:
        return False
    shorter = first if len(first) <= len(second) else second
    longer = second if shorter is first else first
    return longer[: len(shorter)] == shorter


def _system_directories() -> list[Path]:
    """The directories the operating system owns, in this platform's spelling."""
    if os.name != "nt":
        return [Path(root) for root in _SYSTEM_ROOTS]
    found = []
    for name in _SYSTEM_VARIABLES:
        value = os.environ.get(name, "").strip()
        if value and Path(value).is_absolute():
            found.append(Path(value))
    root = os.environ.get("SystemRoot", "").strip()
    if root and Path(root).is_absolute():
        found.append(Path(root) / "System32")
    return found


def _at_volume_root(path: Path) -> bool:
    """Whether the path is a drive root, a share root, or a filesystem root."""
    parent = path.parent
    return parent == path or not parent.anchor or parent == Path(parent.anchor)


def admit(candidate: str | Path, backend) -> Path:
    """The absolute location a new project may be created in, or a refusal.

    The answer is always absolute and is never resolved on disk: the folder may
    not exist yet, and a location that cannot be resolved must be judged by how it
    is spelled rather than admitted because the check could not run.
    """
    raw = str(candidate).strip()
    if not raw or "\x00" in raw:
        raise LibraryLocationRefused("project_location_invalid", "That folder cannot be used.")
    path = Path(raw)
    if not path.is_absolute():
        raise LibraryLocationRefused(
            "project_location_invalid", "Choose a folder on this computer."
        )
    # A trailing separator and a dot component both describe the same directory,
    # and the checks below compare components rather than strings.
    path = Path(os.path.abspath(raw))
    if _at_volume_root(path):
        raise LibraryLocationRefused(
            "project_location_root",
            "Wenyi cannot keep projects at the top of a drive. Choose a folder inside it.",
        )
    home = paths.platform_home()
    if path == home:
        raise LibraryLocationRefused(
            "project_location_profile",
            "Wenyi cannot keep projects directly in your user folder. Choose a folder inside it.",
        )
    for label, message in _owned_roots(backend):
        if _overlaps(path, label):
            raise LibraryLocationRefused(message[0], message[1])
    if any(_overlaps(path, root) for root in _system_directories()):
        raise LibraryLocationRefused(
            "project_location_system",
            "That folder belongs to the operating system. Choose a different folder.",
        )
    return path


def _owned_roots(backend) -> list[tuple[Path, tuple[str, str]]]:
    """The roots this application owns, paired with the sentence for choosing each.

    A root that cannot be resolved is still listed: refusing a choice because one
    of them is unreadable would make the picker depend on a directory the user was
    not choosing.
    """
    owned: list[tuple[Path, tuple[str, str]]] = [
        (
            Path(backend.workspace),
            (
                "project_location_workspace",
                "Wenyi already keeps its own files in that folder. Choose a different folder.",
            ),
        ),
        (
            paths.runtime_root(backend.workspace),
            (
                "project_location_runtime",
                "That folder is inside Wenyi's own cache, which it clears. "
                "Choose a different folder.",
            ),
        ),
    ]
    if backend.library_root is not None:
        owned.append(
            (
                backend.library_root,
                (
                    "project_location_library",
                    "New projects are already created in that folder. "
                    "Choose a folder inside it or a different one.",
                ),
            )
        )
    # The installation is what uninstall and upgrade own, and the reason a chosen
    # location may never be the application's own directory or a parent of it.
    if backend.install_root is not None:
        owned.append(
            (
                backend.install_root,
                (
                    "project_location_install",
                    "That folder is where Wenyi is installed. Choose a different folder.",
                ),
            )
        )
    return owned
