"""Project configuration and exclusive write boundaries shared by API and workers."""

from __future__ import annotations

from contextlib import contextmanager

import yaml
from fastapi import HTTPException
from wenyi_core.config import Config
from wenyi_core.llm.routing import resolve_routes
from wenyi_core.storage.protocol import Storage

from . import dal, paths
from .config_documents import (
    config_document,
    merge_project,
    parse_yaml,
    project_document,
    project_llm,
)
from .context import current_context
from .global_settings import load_settings, registered_models
from .strategies import _saved_strategy_to_config


def require_project(pid: str) -> dict:
    project = dal.get_project(pid)
    if project is None:
        raise HTTPException(404, "project not found")
    return project


def storage_for(pid: str) -> Storage:
    return current_context().storage_for(pid)


def read_storage_for(pid: str) -> Storage:
    """Open project state for inspection without creating a missing state store."""
    return current_context().read_storage_for(pid)


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


def validate_translation_mode_for_format(mode: str, fmt: str | None) -> None:
    """Keep the configured book-only mode consistent with the source workflow."""
    if fmt == "srt" and mode != "standard":
        raise ValueError("Precision translation is only available for books")


def effective_config(
    project: dict, *, document: dict | None = None, defaults: Config | None = None
) -> Config:
    base = defaults if defaults is not None else load_settings().config
    base = _saved_strategy_to_config(
        project.get("strategy") or {"template": "标准翻译"},
        base,
        source_lang=project.get("source_lang") or "auto",
        target_lang=project.get("target_lang") or "zh",
    )
    saved = project.get("config") or {}
    raw = merge_project(config_document(base), saved if document is None else document)
    saved_pipeline = saved.get("pipeline", {})
    if not isinstance(saved_pipeline, dict):
        raise ValueError("Saved pipeline configuration must be a mapping")
    saved_mode = saved_pipeline.get("translation_mode", "standard")
    if document is not None:
        requested_mode = document.get("pipeline", {}).get("translation_mode", saved_mode)
        if requested_mode != saved_mode:
            raise ValueError("Create a new project to change the translation mode")
    raw["pipeline"]["translation_mode"] = saved_mode
    if saved_mode == "best_of_three" and (
        document is not None and "polish" not in document.get("pipeline", {})
    ):
        raw["pipeline"]["polish"] = True
    raw["paths"] = {"state_dir": paths.project_dir(project["id"])}
    config = Config.from_dict(raw)
    validate_translation_mode_for_format(config.pipeline.translation_mode, project.get("fmt"))
    if config.source_lang == config.target_lang:
        raise ValueError("Source and target languages are identical; choose another direction")
    if project.get("initialized"):
        if config.target_lang != project.get("target_lang"):
            raise ValueError(
                "Create a new project to change the target language after initialization"
            )
        if config.source_lang not in {"auto", project.get("source_lang")}:
            raise ValueError("Source language conflicts with initialized project")
        resolved = Config.model_validate(
            {**config.model_dump(), "source_lang": project["source_lang"]}
        )
    else:
        resolved = config
    resolved.language_policy("translation", path="srt" if project.get("fmt") == "srt" else "book")
    return config


def parse_project_yaml(value: str) -> dict:
    raw = parse_yaml(value)
    pipeline = raw.get("pipeline")
    if isinstance(pipeline, dict) and "precision_concurrency" in pipeline:
        raise ValueError(
            "Initial-draft concurrency is built in; remove pipeline.precision_concurrency"
        )
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
