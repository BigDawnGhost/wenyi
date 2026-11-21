"""Global configuration contract and platform-independent validation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from wenyi_core.config import Config
from wenyi_core.llm.configuration import split_model_ref
from wenyi_core.llm.registry import provider_spec

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
            "translation_mode, prompt_genre and glossary_note_workers are project settings; choose them at creation."
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
    result = {}
    for ref, settings in config.llm.models.items():
        provider, model = split_model_ref(ref)
        connection = config.llm.providers[provider]
        spec = provider_spec(connection.kind)
        adapter = spec.adapter_type()
        control = adapter.route_thinking(connection, model)
        choices = []
        if control:
            for level in control.choices(settings.options):
                options = spec.validate_model(
                    settings, options=control.apply(settings.options, level)
                )
                try:
                    adapter.output_limit(options, None, settings.max_output_tokens)
                except ValueError:
                    continue
                choices.append(level)
        result[ref] = {
            "provider": provider,
            "model": model,
            "thinking_levels": choices,
            "thinking_value": (
                control.read(spec.validate_model(settings).model_dump())
                if control and choices
                else None
            ),
        }
    return result
