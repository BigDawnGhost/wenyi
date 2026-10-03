"""Organize uploaded originals and exported files within the data volume."""

from __future__ import annotations

import os

from .context import current_context


def data_dir() -> str:
    return current_context().data_dir


def project_dir(project_id: str) -> str:
    return str(current_context().project_dir(project_id))


def source_path(project_id: str, fmt: str) -> str:
    ext = {
        "epub": "epub",
        "text": "txt",
        "fb2": "fb2",
        "html": "html",
        "pdf": "pdf",
    }.get(fmt, fmt or "bin")
    return os.path.join(project_dir(project_id), f"source.{ext}")


def source_cache_dir(project_id: str) -> str:
    d = os.path.join(project_dir(project_id), "source")
    os.makedirs(d, exist_ok=True)
    return d


def exports_dir(project_id: str) -> str:
    d = os.path.join(project_dir(project_id), "exports")
    os.makedirs(d, exist_ok=True)
    return d
