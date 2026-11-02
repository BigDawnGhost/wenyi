"""Filesystem naming rules a title has to be reduced to before it becomes a path.

A title is user text, so it can name a device ("CON"), end in a dot or a space,
or be nothing at all. Windows resolves a reserved name from the part before the
first dot and silently discards trailing dots and spaces, so a name that keeps
them identifies a path the system does not have. Repairing the name here means
one rule for every caller instead of one check per writer.

These are pure functions on purpose. The development drive is ReFS, which
accepts names NTFS refuses, so a test that relied on the directory call failing
would be green here and red on a user's machine.
"""

from __future__ import annotations

#: Device names Windows reserves in every directory, in both cases.
RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)

#: Fallback used when a title reduces to nothing nameable.
DEFAULT_COMPONENT = "book"


def strip_trailing(text: str) -> str:
    """Remove the trailing dots and spaces Windows discards when creating a name."""
    return text.rstrip(" .")


def escape_reserved(name: str) -> str:
    """Prefix a reserved device name so it can name a file or directory.

    Only the stem is compared, because Windows resolves "CON" and "CON.txt" to
    the same device. "CONSOLE" and "COM10" are ordinary names and stay as they are.
    """
    stem = name.split(".", 1)[0].upper()
    return f"_{name}" if stem in RESERVED_NAMES else name


def safe_component(name: str, *, fallback: str = DEFAULT_COMPONENT) -> str:
    """Reduce one name to a component Windows can create, or return the fallback.

    The trailing strip runs after the reserved-name escape so an escaped name is
    not re-normalised back into the form the escape avoided.
    """
    cleaned = strip_trailing(escape_reserved(strip_trailing(name)))
    return cleaned or fallback
