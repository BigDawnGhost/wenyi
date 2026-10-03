"""Map workflow choices to the same validated configuration as the CLI."""

from __future__ import annotations

from typing import Any

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
        "name": "风格与初始术语",
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
    {
        "id": "report",
        "name": "生成报告",
        "category": "post_process",
        "always_on": True,
        "locked": True,
    },
]
_SWITCHES = {"book_understanding", "polish", "annotation_alignment", "review", "review_autofix"}
_STANDARD = {**dict.fromkeys(sorted(_SWITCHES), True), "punctuation_normalize": True}
_QUICK = {
    **_STANDARD,
    "book_understanding": False,
    "polish": False,
    "review": False,
    "review_autofix": False,
}
PRESET_TEMPLATES = [
    {
        "name": "标准翻译",
        "description": "与 dev 默认流程一致：预理解、润色、审校及自动修复",
        "time_factor": 2,
        "recommended": True,
        "steps": _STANDARD,
    },
    {
        "name": "快速出稿",
        "description": "关闭预理解、润色和审校，保留正文与术语翻译",
        "time_factor": 1,
        "steps": _QUICK,
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
    strategy: dict[str, Any], base: Config, *, source_lang: str = "auto", target_lang: str = "zh"
) -> Config:
    unknown = set(strategy) - {"template", "steps", "description", "time_factor"}
    if unknown:
        raise ValueError("Unknown strategy fields: " + ", ".join(sorted(unknown)))
    cfg = base.model_copy(deep=True)
    cfg.source_lang = source_lang
    cfg.target_lang = target_lang
    steps = strategy.get("steps")
    if steps is None:
        name = strategy.get("template", "标准翻译")
        definition = builtin_template_definition(name)
        if definition is None:
            raise ValueError(f"Unknown strategy template: {name}")
        steps = {} if name == "标准翻译" else definition["steps"]
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
    if not cfg.pipeline.review:
        cfg.pipeline.review_autofix = False
    return Config.model_validate(cfg.model_dump())


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
