"""Map workflow choices to the same validated configuration as the CLI."""

from __future__ import annotations

from typing import Any, Literal

from wenyi_core.config import Config

STEP_REGISTRY = [
    {
        "id": "language_detection",
        "name": "语言检测",
        "category": "prepare",
        "always_on": True,
        "locked": True,
    },
    {
        "id": "style_analysis",
        "name": "风格分析",
        "category": "prepare",
        "always_on": True,
        "locked": True,
    },
    {"id": "book_understanding", "name": "全书预理解", "category": "prepare"},
    {
        "id": "batch_translate",
        "name": "批次翻译",
        "category": "per_chapter",
        "always_on": True,
        "locked": True,
    },
    {
        "id": "polish",
        "name": "翻译润色",
        "category": "per_chapter",
        "depends_on": ["batch_translate"],
    },
    {
        "id": "annotation_alignment",
        "name": "注释定位",
        "category": "per_chapter",
        "depends_on": ["batch_translate"],
    },
    {
        "id": "term_extract",
        "name": "实时术语提取",
        "category": "per_chapter",
        "always_on": True,
        "locked": True,
    },
    {
        "id": "review",
        "name": "全书审校",
        "category": "post_process",
        "depends_on": ["batch_translate"],
    },
    {
        "id": "review_autofix",
        "name": "发布审校修订",
        "category": "post_process",
        "depends_on": ["review"],
    },
    {"id": "punctuation_normalize", "name": "导出标点规范化", "category": "export"},
]
_SWITCHES = {"book_understanding", "polish", "annotation_alignment", "review", "review_autofix"}
_STANDARD = {**dict.fromkeys(sorted(_SWITCHES), True), "punctuation_normalize": True}
PRESET_TEMPLATES = [
    {
        "name": "标准翻译",
        "description": "与 dev 默认流程一致：预理解、润色、审校及自动修复",
        "time_factor": 2,
        "recommended": True,
        "steps": _STANDARD,
    },
]


def builtin_template_definition(name: str) -> dict[str, Any] | None:
    for template in PRESET_TEMPLATES:
        if template["name"] == name:
            return {
                "template": name,
                **{key: value for key, value in template.items() if key != "name"},
            }
    return None


def strategy_to_config(
    strategy: dict[str, Any],
    base: Config,
    *,
    source_lang: str = "auto",
    target_lang: str = "zh",
    translation_mode: Literal["standard", "three_draft_synthesis"] | None = None,
) -> Config:
    unknown = set(strategy) - {"template", "steps", "description", "time_factor"}
    if unknown:
        raise ValueError("Unknown strategy fields: " + ", ".join(sorted(unknown)))
    if builtin_template_definition(strategy.get("template", "标准翻译")) is None:
        raise ValueError(f"Unknown strategy template: {strategy.get('template')}")
    cfg = base.model_copy(deep=True)
    cfg.source_lang = source_lang
    cfg.target_lang = target_lang
    steps = strategy.get("steps")
    if steps is None:
        steps = {}
    if not isinstance(steps, dict):
        raise ValueError("Strategy steps must be an object")
    allowed = {step["id"] for step in STEP_REGISTRY}
    for key, value in steps.items():
        if key not in allowed:
            raise ValueError(f"Unsupported workflow step: {key}")
        if not isinstance(value, bool):
            raise ValueError(f"Workflow step {key} must be boolean")
        if key in _SWITCHES:
            setattr(cfg.pipeline, key, value)
        elif key == "punctuation_normalize":
            cfg.output.punctuation_normalize = value
        elif not value:
            raise ValueError(f"Required workflow step cannot be disabled: {key}")
    if translation_mode is not None:
        cfg.pipeline.translation_mode = translation_mode
        if translation_mode == "three_draft_synthesis":
            cfg.pipeline.polish = True
    if not cfg.pipeline.review:
        cfg.pipeline.review_autofix = False
    validated = Config.model_validate(cfg.model_dump())
    validated.set_prompt_genre(base.prompt_genre)
    return validated


def workflow_steps(config: Config) -> dict[str, bool]:
    """Describe the actual switches for a configured workflow template."""
    return {
        step["id"]: (
            bool(getattr(config.pipeline, step["id"]))
            if step["id"] in _SWITCHES
            else config.output.punctuation_normalize
            if step["id"] == "punctuation_normalize"
            else True
        )
        for step in STEP_REGISTRY
    }
