"""Global configuration contract and platform-independent validation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from wenyi_core.config import Config

from .config_documents import PROJECT_PIPELINE_FIELDS, parse_yaml
from .context import current_context
from .strategies import PRESET_TEMPLATES


@dataclass(frozen=True)
class GlobalSettings:
    config: Config
    default_template: str = "标准翻译"
    revision: int = 0


def load_settings(*, connection: Any = None) -> GlobalSettings:
    return current_context().settings_store.load(connection=connection)


def registry_guard(*, exclusive: bool = False):
    return current_context().settings_store.guard(exclusive=exclusive)


def validate_settings(value: str, default_template: str) -> Config:
    if default_template not in {template["name"] for template in PRESET_TEMPLATES}:
        raise ValueError("Unknown default workflow template")
    document = parse_yaml(value)
    pipeline = document.get("pipeline", {})
    if isinstance(pipeline, dict) and PROJECT_PIPELINE_FIELDS.intersection(pipeline):
        raise ValueError(
            "translation_mode is a project setting; choose it when creating a project. "
            "Initial-draft concurrency is built in, not configurable."
        )
    config = Config.from_dict(document)
    if config.source_lang == config.target_lang:
        raise ValueError("Source and target languages are identical")
    return config


def save_settings(
    value: str, default_template: str, revision: int, **kwargs: Any
) -> GlobalSettings:
    return current_context().settings_store.save(value, default_template, revision, **kwargs)


def registered_models(config: Config) -> dict[str, Any]:
    return {
        key: {"model": profile.model, "provider": profile.provider}
        for key, profile in config.llm.models.items()
    }
