"""Pure reconstruction of saved global/project settings; platforms own backup and commit."""

from copy import deepcopy
from typing import Any

from wenyi_core.config import Config
from wenyi_core.config_rebuild import RebuiltConfig, rebuild_config
from wenyi_core.i18n.genres import require_genre

from .config_documents import PROJECT_LLM_FIELDS, PROJECT_PIPELINE_FIELDS, config_document
from .strategies import PRESET_TEMPLATES, builtin_template_definition, strategy_to_config


def rebuild_default_template(value: Any) -> str:
    """Use a current template or the default without interpreting historical names."""
    return value if builtin_template_definition(value) is not None else PRESET_TEMPLATES[0]["name"]


def rebuild_strategy(raw: Any) -> RebuiltConfig:
    """Reset invalid saved workflow selections; explicit project configuration stays separate."""
    if raw is None:
        return RebuiltConfig({}, ())
    if isinstance(raw, dict):
        try:
            strategy_to_config(raw, Config())
        except (ValueError, TypeError):
            pass
        else:
            return RebuiltConfig(deepcopy(raw), ())
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
