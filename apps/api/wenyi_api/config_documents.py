"""Separate editable project selections from complete execution snapshots."""

from __future__ import annotations

from typing import Any

import yaml
from wenyi_core.config import Config

PROJECT_LLM_FIELDS = frozenset({"tiers", "routes", "budget"})


def config_document(config: Config) -> dict[str, Any]:
    """Serialize all runtime settings, including resolved model definitions."""
    return {
        "language": {"source": config.source_lang, "target": config.target_lang},
        "llm": config.llm.model_dump(mode="json"),
        "segment": config.segment.model_dump(mode="json"),
        "pipeline": config.pipeline.model_dump(mode="json"),
        "output": config.output.model_dump(mode="json"),
        "honorific": {"strategy": config.honorific_strategy},
    }


def project_document(config: Config) -> dict[str, Any]:
    document = config_document(config)
    document["llm"] = {key: document["llm"][key] for key in sorted(PROJECT_LLM_FIELDS)}
    return document


def parse_yaml(value: str) -> dict:
    raw = yaml.safe_load(value)
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("Configuration must be a mapping")
    if "paths" in raw:
        raise ValueError("Web storage paths are managed by the server")
    return raw


def project_llm(value: Any, *, strict: bool = False) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Configuration section llm must be a mapping")
    if strict and set(value) - PROJECT_LLM_FIELDS:
        raise ValueError(
            "Register providers and models in global Settings; projects may only set "
            "llm.tiers, llm.routes and llm.budget"
        )
    return {key: item for key, item in value.items() if key in PROJECT_LLM_FIELDS}


def merge_project(base: dict, override: dict) -> dict:
    """Apply project choices while keeping the shared registry authoritative."""
    result = {**base}
    for section, value in override.items():
        if not isinstance(value, dict):
            raise ValueError(f"Configuration section {section} must be a mapping")
        if section == "llm":
            value = project_llm(value)
            for key, item in value.items():
                if not isinstance(item, dict):
                    raise ValueError(f"llm.{key} must be a mapping")
        result[section] = {**base.get(section, {}), **value}
        if section == "llm" and "tiers" in value:
            result[section]["tiers"] = {**base[section]["tiers"], **value["tiers"]}
    return result
