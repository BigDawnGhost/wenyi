"""Reconstruct saved settings without writing them; platforms own backup and commit."""

from copy import deepcopy
from pathlib import Path
from typing import Any

from wenyi_core.config import Config, parse_config_yaml
from wenyi_core.config_rebuild import RebuiltConfig, rebuild_config
from wenyi_core.i18n.genres import require_genre

from .config_documents import PROJECT_LLM_FIELDS, PROJECT_PIPELINE_FIELDS, config_document
from .strategies import PRESET_TEMPLATES, builtin_template_definition, strategy_to_config


def read_bootstrap_settings(path: str | Path) -> Any:
    """Read the configured seed without changing it; startup can back up the original."""
    document = parse_config_yaml(Path(path).read_text(encoding="utf-8"))
    return {} if document is None else document


def load_bootstrap_defaults(path: str | Path) -> Config:
    """Resolve defaults from the saved seed without writing the YAML or active settings."""
    return Config.from_dict(rebuild_settings(read_bootstrap_settings(path)).document)


def rebuild_default_template(value: Any) -> str:
    """Use a current template or the default without interpreting historical names."""
    return value if builtin_template_definition(value) is not None else PRESET_TEMPLATES[0]["name"]


def rebuild_strategy(raw: Any) -> RebuiltConfig:
    """Discard retired export settings before validating saved workflow selections."""
    if raw is None:
        return RebuiltConfig({}, ())
    if isinstance(raw, dict):
        document = deepcopy(raw)
        removed = ()
        steps = document.get("steps")
        if isinstance(steps, dict) and "punctuation_normalize" in steps:
            steps.pop("punctuation_normalize")
            removed = ("strategy.steps.punctuation_normalize",)
        try:
            strategy_to_config(document, Config())
        except (ValueError, TypeError):
            pass
        else:
            return RebuiltConfig(document, removed)
    return RebuiltConfig({}, ("strategy",))


def rebuild_settings(
    raw: Any, *, defaults: Config | None = None, project: bool = False
) -> RebuiltConfig:
    """Preserve valid selections without making old names or removed fields into aliases."""
    if not isinstance(raw, dict):
        return RebuiltConfig({}, ("configuration",))
    document = deepcopy(raw)
    removed = []
    if "paths" in document:
        document.pop("paths")
        removed.append("paths")
    pipeline = document.get("pipeline")
    genre = None
    if isinstance(pipeline, dict):
        if project and "prompt_genre" in pipeline:
            value = pipeline.pop("prompt_genre")
            try:
                genre = require_genre(value)
            except (ValueError, TypeError):
                removed.append("pipeline.prompt_genre")
        elif not project:
            for key in PROJECT_PIPELINE_FIELDS:
                if key in pipeline:
                    pipeline.pop(key)
                    removed.append(f"pipeline.{key}")
    llm = document.get("llm")
    if project and isinstance(llm, dict):
        for key in list(llm):
            if key not in PROJECT_LLM_FIELDS:
                llm.pop(key)
                removed.append(f"llm.{key}")
    rebuilt = rebuild_config(
        document, base=config_document(defaults or Config()) if project else None
    )
    if genre is not None:
        rebuilt.document.setdefault("pipeline", {})["prompt_genre"] = genre
    return RebuiltConfig(rebuilt.document, (*removed, *rebuilt.removed))
