"""Read packaged rules with wheel and PyInstaller support."""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from importlib.resources import files
from typing import Any


@lru_cache(maxsize=None)
def read_text(path: str) -> str:
    """Read a fixed relative resource path independently of the working directory."""
    parts = path.split("/")
    if any(not part or part in {".", ".."} or "\\" in part for part in parts):
        raise ValueError(f"Invalid language resource path: {path}")
    resource = files("trans_novel.i18n").joinpath("data")
    for part in parts:
        resource = resource.joinpath(part)
    return resource.read_text(encoding="utf-8")


def read_json(path: str) -> dict[str, Any]:
    """Return an independent dictionary so callers cannot mutate shared rules."""
    value = json.loads(read_text(path))
    if not isinstance(value, dict):
        raise ValueError(f"Language resource must be a JSON object: {path}")
    return value


@lru_cache(maxsize=1)
def prompt_fingerprint() -> str:
    """Fingerprint packaged prompts and language rules, excluding export about pages."""
    digest = hashlib.sha256()

    def visit(folder, prefix: str) -> None:
        for entry in sorted(folder.iterdir(), key=lambda item: item.name):
            relative = f"{prefix}{entry.name}"
            if entry.is_dir():
                visit(entry, relative + "/")
            elif entry.name.endswith((".txt", ".json")):
                digest.update(relative.encode("utf-8") + b"\0")
                digest.update(read_text(relative).encode("utf-8") + b"\0")

    root = files("trans_novel.i18n").joinpath("data")
    for name in ("languages", "pairs", "shared", "tasks"):
        visit(root.joinpath(name), name + "/")
    return digest.hexdigest()
