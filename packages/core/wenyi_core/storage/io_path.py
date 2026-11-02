"""Extended-length path spelling for every filesystem call.

Windows rejects a path over `MAX_PATH` unless it is spelled with the extended
length prefix. Having one implementation keeps every reader and writer in the
same namespace: two spellings of one directory are not interchangeable, and
mixing them makes containment checks and locks disagree about which file they
opened. The prefix does not apply to SQLite, which needs a length check instead;
see `require_sqlite_path`.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

#: Longest path SQLite can open, in characters. `sqlite3_open_v2` does not accept
#: the extended-length prefix, so the legacy limit is the hard ceiling.
SQLITE_PATH_LIMIT = 259


def to_io_path(path: str | Path) -> str:
    """Use absolute extended-length drive/UNC paths on Windows; leave POSIX unchanged."""
    text = str(path)
    if os.name != "nt":
        return text
    text = os.path.abspath(text)
    if text.startswith("\\\\?\\"):
        return text
    if text.startswith("\\\\"):
        return "\\\\?\\UNC\\" + text[2:]
    return "\\\\?\\" + text


def plain_path(path: str | Path) -> str:
    """Undo `to_io_path` so a path can be reported or compared the way it was named.

    Callers that show a path to a person, or that compare it against a spelling
    arriving from configuration or the native side, need the ordinary form. The
    extended-length form is an implementation detail of opening the file.

    Only a drive letter and a UNC share are unwrapped. A device namespace and a
    volume GUID stay as written, because neither has an ordinary spelling.
    """
    text = str(path)
    if text.startswith("\\\\?\\UNC\\"):
        return "\\\\" + text[8:]
    if re.match(r"^\\\\\?\\[A-Za-z]:\\", text):
        return text[4:]
    return text


def require_sqlite_path(path: str | Path) -> str:
    """Return a database path SQLite can open, or explain why it cannot.

    SQLite rejects `\\\\?\\` in a URI, so a long database path cannot be rescued by
    the prefix the way an ordinary file can. Failing here, before a connection
    is attempted, turns an opaque `unable to open database file` into a message
    that names the limit and the length actually requested.
    """
    text = plain_path(path)
    length = len(os.path.abspath(text))
    if length > SQLITE_PATH_LIMIT:
        raise ValueError(
            f"SQLite cannot open a path longer than {SQLITE_PATH_LIMIT} characters "
            f"({length} given): {text}"
        )
    return text
