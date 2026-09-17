"""Project configuration and exclusive write boundaries shared by API and workers."""

from __future__ import annotations

from contextlib import contextmanager

import yaml
from fastapi import HTTPException
from wenyi_core.config import Config
from wenyi_core.llm.routing import resolve_routes

from . import dal, paths
from .config_documents import (
    config_document,
    merge_project,
    parse_yaml,
    project_document,
    project_llm,
)
from .db import get_pool
from .global_settings import load_settings, registered_models
from .storage_pg import PostgresStorage
from .strategies import strategy_to_config


def require_project(pid: str) -> dict:
    project = dal.get_project(pid)
    if project is None:
        raise HTTPException(404, "project not found")
    return project


def storage_for(pid: str) -> PostgresStorage:
    return PostgresStorage(pid, get_pool(), run_dir=paths.project_dir(pid))


def busy(project: dict) -> bool:
    return project.get("status") in dal.RUNNING_PROJECT_STATUSES


@contextmanager
def project_write(pid: str):
    """Serialize API writes against queued and running domain operations."""
    require_project(pid)
    storage = storage_for(pid)
    try:
        with storage.lock(blocking=False):
            project = require_project(pid)
            if busy(project):
                raise HTTPException(409, "project already has a running task")
            yield project, storage
    except BlockingIOError as error:
        raise HTTPException(409, "project already has a running task") from error


def effective_config(
    project: dict, *, document: dict | None = None, defaults: Config | None = None
) -> Config:
    base = defaults if defaults is not None else load_settings().config
    base = strategy_to_config(
        project.get("strategy") or {"template": "标准翻译"},
        base,
        source_lang=project.get("source_lang") or "auto",
        target_lang=project.get("target_lang") or "zh",
    )
    saved = project.get("config") or {}
    raw = merge_project(config_document(base), saved if document is None else document)
    raw["paths"] = {"state_dir": paths.project_dir(project["id"])}
    config = Config.from_dict(raw)
    if config.source_lang == config.target_lang:
        raise ValueError("Source and target languages are identical; choose another direction")
    if project.get("initialized"):
        if config.target_lang != project.get("target_lang"):
            raise ValueError(
                "Create a new project to change the target language after initialization"
            )
        if config.source_lang not in {"auto", project.get("source_lang")}:
            raise ValueError("Source language conflicts with initialized project")
    return config


def parse_project_yaml(value: str) -> dict:
    raw = parse_yaml(value)
    if "llm" in raw:
        project_llm(raw["llm"], strict=True)
    return raw


def config_response(project: dict, config: Config) -> dict:
    document = project_document(config)
    return {
        "yaml": yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
        "effective": document,
        "routes": [route.describe() for route in resolve_routes(config.llm).values()],
        "editable": not busy(project),
        "registered_models": registered_models(config),
    }


def require_book(project: dict) -> None:
    if project.get("fmt") == "srt":
        raise HTTPException(422, "This operation is only available for book projects")
