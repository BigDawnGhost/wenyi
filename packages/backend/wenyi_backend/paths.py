"""Organize uploaded originals and exported files within the data volume."""

from __future__ import annotations

import os

from .context import current_context


def data_dir() -> str:
    return current_context().data_dir


def project_dir(project_id: str) -> str:
    return str(current_context().project_dir(project_id))


def exports_dir(project_id: str) -> str:
    d = os.path.join(project_dir(project_id), "exports")
    os.makedirs(d, exist_ok=True)
    return d
