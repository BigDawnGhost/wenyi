"""The optional ``.env`` file that sits beside ``config.yaml``.

Wenyi reads every credential from the environment. This module owns the one file
``wenyi model`` writes when it obtains a credential, so a later shell needs no manual
``export``. Loading never overrides a variable the caller already exported, and the file is
kept owner-readable only.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path

ENV_FILE_NAME = ".env"
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def env_file_path(config_path: str | os.PathLike[str]) -> Path:
    """Return the credential file that belongs to one configuration file."""
    return Path(config_path).expanduser().resolve().parent / ENV_FILE_NAME


def parse_env_line(line: str) -> tuple[str, str] | None:
    """Return the ``(key, value)`` one line assigns, or ``None`` for blanks and comments."""
    text = line.strip()
    if not text or text.startswith("#"):
        return None
    if text.startswith("export "):
        text = text[len("export ") :].lstrip()
    key, separator, raw = text.partition("=")
    key = key.strip()
    if not separator or not _ENV_KEY.match(key):
        return None
    return key, _unquote(raw.strip())


def _unquote(raw: str) -> str:
    """Read one quoted value, including the shell concatenation form ``'a'\\''b'``.

    A value that carries no quotes loses everything from a trailing `` #`` comment.
    """
    if raw[:1] not in {"'", '"'}:
        return raw.split(" #", 1)[0].rstrip()
    value: list[str] = []
    quote = ""
    index = 0
    while index < len(raw):
        char = raw[index]
        if quote:
            if char == quote:
                quote = ""
                index += 1
                continue
            if quote == '"' and char == "\\" and index + 1 < len(raw):
                value.append(raw[index + 1])
                index += 2
                continue
        elif char in {"'", '"'}:
            quote = char
            index += 1
            continue
        elif char == "\\" and index + 1 < len(raw):
            value.append(raw[index + 1])
            index += 2
            continue
        value.append(char)
        index += 1
    return "".join(value)


def quote_env_value(value: str) -> str:
    """Return one value as a single-quoted payload that keeps shell metacharacters literal."""
    return "'" + value.replace("'", "'\\''") + "'"


def read_env_values(path: str | os.PathLike[str]) -> dict[str, str]:
    """Read every assignment one credential file declares; a missing file reads as empty."""
    target = Path(path)
    if not target.is_file():
        return {}
    values: dict[str, str] = {}
    for line in target.read_text(encoding="utf-8").splitlines():
        parsed = parse_env_line(line)
        if parsed is not None:
            values[parsed[0]] = parsed[1]
    return values


def write_env_values(path: str | os.PathLike[str], values: Mapping[str, str]) -> None:
    """Update or append assignments, keep every other line, then replace the file atomically."""
    for key in values:
        if not _ENV_KEY.match(key):
            raise ValueError(f"{key!r} is not an environment variable name")
    for key, value in values.items():
        if "\n" in value or "\r" in value:
            raise ValueError(f"The value for {key} must be a single line")
    target = Path(path)
    lines = target.read_text(encoding="utf-8").splitlines() if target.is_file() else []
    written: set[str] = set()
    kept: list[str] = []
    for line in lines:
        parsed = parse_env_line(line)
        if parsed is None or parsed[0] not in values:
            kept.append(line)
            continue
        key = parsed[0]
        if key not in written:
            written.add(key)
            kept.append(f"{key}={quote_env_value(values[key])}")
    for key, value in values.items():
        if key not in written:
            kept.append(f"{key}={quote_env_value(value)}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=target.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.write("\n".join(kept) + "\n")
    try:
        # A credential file holds secrets: replace it as owner-only, whatever it was before.
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def load_env_file(path: str | os.PathLike[str], *, override: bool = False) -> tuple[str, ...]:
    """Export one credential file into ``os.environ`` and return the names it set."""
    loaded: list[str] = []
    for key, value in read_env_values(path).items():
        if override or not os.environ.get(key, "").strip():
            os.environ[key] = value
            loaded.append(key)
    return tuple(loaded)


def export_lines(values: Mapping[str, str]) -> str:
    """Return copy-pasteable ``export`` lines, without touching any file."""
    return "\n".join(f"export {key}={quote_env_value(value)}" for key, value in values.items())


__all__ = [
    "ENV_FILE_NAME",
    "env_file_path",
    "export_lines",
    "load_env_file",
    "parse_env_line",
    "quote_env_value",
    "read_env_values",
    "write_env_values",
]
