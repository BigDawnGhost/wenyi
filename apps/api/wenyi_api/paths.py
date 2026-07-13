"""文件路径工具：上传原件 / 导出成品 在 data/ 卷下的组织。"""

from __future__ import annotations

import os

from .config import settings


def project_dir(project_id: str) -> str:
    d = os.path.join(settings.data_dir, project_id)
    os.makedirs(d, exist_ok=True)
    return d


def source_path(project_id: str, fmt: str) -> str:
    ext = {"epub": "epub", "text": "txt", "fb2": "fb2"}.get(fmt, fmt or "bin")
    return os.path.join(project_dir(project_id), f"source.{ext}")


def exports_dir(project_id: str) -> str:
    d = os.path.join(project_dir(project_id), "exports")
    os.makedirs(d, exist_ok=True)
    return d
