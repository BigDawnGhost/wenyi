"""Project configuration and exclusive write boundaries shared by API and workers."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager

import yaml
from wenyi_core.config import Config
from wenyi_core.i18n.genres import DEFAULT_GENRE
from wenyi_core.llm.routing import resolve_routes

from .config_documents import (
    config_document,
    config_from_document,
    merge_project,
    parse_yaml,
    project_document,
    project_llm,
)
from .context import BackendStorage
from .errors import PROJECT_BUSY, PROJECT_NOT_FOUND, ApiError
from .global_settings import registered_models
from .ports.projects import RUNNING_PROJECT_STATUSES, ProjectReader
from .strategies import strategy_to_config


def require_project(pid: str, projects: ProjectReader) -> dict:
    project = projects.get_project(pid)
    if project is None:
        raise ApiError(PROJECT_NOT_FOUND, 404, "project not found")
    return project


def busy(project: dict) -> bool:
    return project.get("status") in RUNNING_PROJECT_STATUSES


@contextmanager
def project_write(pid: str, projects: ProjectReader, storage_for: Callable[[str], BackendStorage]):
    """Serialize API writes against queued and running domain operations."""
    require_project(pid, projects)
    storage = storage_for(pid)
    try:
        with storage.lock(blocking=False):
            project = require_project(pid, projects)
            if busy(project):
                raise ApiError(PROJECT_BUSY, 409, "project already has a running task")
            yield project, storage
    except BlockingIOError as error:
        raise ApiError(PROJECT_BUSY, 409, "project already has a running task") from error
    finally:
        storage.close()


def effective_config(
    project: dict,
    *,
    document: dict | None = None,
    defaults: Config,
    state_dir: str,
) -> Config:
    base = defaults
    base = strategy_to_config(
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
    for field, default, label in (
        ("translation_mode", "standard", "translation mode"),
        ("prompt_genre", DEFAULT_GENRE, "prompt genre"),
    ):
        saved_value = saved_pipeline.get(field, default)
        if document is not None:
            requested_value = document.get("pipeline", {}).get(field, saved_value)
            if requested_value != saved_value:
                raise ValueError(f"Create a new project to change the {label}")
        raw["pipeline"][field] = saved_value
    if saved_mode == "three_draft_synthesis" and (
        document is not None and "polish" not in document.get("pipeline", {})
    ):
        raw["pipeline"]["polish"] = True
    raw["paths"] = {"state_dir": state_dir}
    config = config_from_document(raw)
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
        resolved.set_prompt_genre(config.prompt_genre)
    else:
        resolved = config
    resolved.language_policy("translation")
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
