"""翻译策略：预设模板 + 策略 → wenyi-core Config 映射。

尊重现有内核流程（tech-stack §A4）：14 个 PRD "步骤"映射到引擎真实的 config 旋钮，
3 个预设模板 = 具体 Config 组合。策略以 JSONB 存储。
"""

from __future__ import annotations

from typing import Any

from wenyi_core.config import Config

# ── 步骤注册表（展示层；映射到 config 旋钮）──────────────────────────────
# locked=True 的步骤不可关闭；always_on=True 的步骤内核恒开（无 config 旋钮）。
STEP_REGISTRY: list[dict[str, Any]] = [
    # prepare
    {"id": "language_detection", "name": "语言检测", "category": "prepare",
     "always_on": True, "locked": True, "depends_on": []},
    {"id": "style_analysis", "name": "风格分析", "category": "prepare",
     "always_on": True, "locked": True, "depends_on": [],
     "output": "风格指南 + 角色列表"},
    {"id": "book_understanding", "name": "书籍预理解", "category": "prepare",
     "always_on": False, "locked": False, "depends_on": [],
     "config": "pipeline.book_understanding", "output": "章节摘要 + 全书概要"},
    {"id": "initial_glossary", "name": "初始术语提取", "category": "prepare",
     "always_on": True, "locked": True, "depends_on": ["style_analysis"]},
    # per_chapter
    {"id": "batch_translate", "name": "批次翻译", "category": "per_chapter",
     "always_on": True, "locked": True, "group": "core", "depends_on": []},
    {"id": "polish", "name": "翻译润色", "category": "per_chapter",
     "always_on": False, "locked": False, "group": "enhance",
     "config": "pipeline.polish", "depends_on": ["batch_translate"],
     "description": "更流畅，但更慢更贵"},
    {"id": "punctuation_normalize", "name": "标点规范化", "category": "per_chapter",
     "always_on": False, "locked": False, "group": "enhance",
     "config": "punctuation_normalize", "depends_on": ["batch_translate"],
     "description": "半角→全角、日式引号→中文引号"},
    {"id": "term_extract", "name": "实时术语提取", "category": "per_chapter",
     "always_on": True, "locked": True, "group": "enhance", "depends_on": ["batch_translate"]},
    {"id": "chapter_review", "name": "章节审校", "category": "per_chapter",
     "always_on": False, "locked": False, "group": "quality",
     "config": "pipeline.review", "depends_on": ["batch_translate"],
     "description": "AI 检查漏译/误译/术语问题"},
    {"id": "autofix", "name": "自动修复", "category": "per_chapter",
     "always_on": False, "locked": False, "group": "quality",
     "config": "pipeline.autofix_severe", "depends_on": ["chapter_review"],
     "description": "对严重问题自动重翻"},
    {"id": "backtranslate", "name": "回译抽检", "category": "per_chapter",
     "always_on": False, "locked": False, "group": "quality",
     "config": "pipeline.backtranslate_sample", "depends_on": ["batch_translate"],
     "options": {"sample_rate": {"default": 0.05, "type": "float"}},
     "description": "抽样回译对比语义偏差"},
    # post_process
    {"id": "title_translate", "name": "标题翻译", "category": "post_process",
     "always_on": True, "locked": True, "depends_on": []},
    {"id": "consistency_qa", "name": "一致性检查", "category": "post_process",
     "always_on": False, "locked": False, "config": "pipeline.consistency_qa",
     "depends_on": [], "description": "跨章检查术语/人称/语气"},
    {"id": "qa_report", "name": "生成 QA 报告", "category": "post_process",
     "always_on": True, "locked": True, "depends_on": []},
]


def _quick() -> dict[str, bool | float]:
    return {
        "book_understanding": False, "polish": False,
        "punctuation_normalize": True, "chapter_review": False,
        "autofix": False, "backtranslate": 0.0, "consistency_qa": False,
    }


def _standard() -> dict[str, bool | float]:
    return {
        "book_understanding": True, "polish": True,
        "punctuation_normalize": True, "chapter_review": True,
        "autofix": False, "backtranslate": 0.0, "consistency_qa": False,
    }


def _premium() -> dict[str, bool | float]:
    return {
        "book_understanding": True, "polish": True,
        "punctuation_normalize": True, "chapter_review": True,
        "autofix": True, "backtranslate": 0.05, "consistency_qa": True,
    }


PRESET_TEMPLATES = [
    {"name": "快速出稿", "description": "速度优先，适合初稿或大批量",
     "time_factor": 1, "steps": _quick()},
    {"name": "标准翻译", "description": "质量与速度平衡（推荐）",
     "time_factor": 2, "steps": _standard(), "recommended": True},
    {"name": "精翻", "description": "质量优先，全部步骤开启",
     "time_factor": 4, "steps": _premium()},
]


def builtin_template_definition(name: str) -> dict[str, Any] | None:
    for t in PRESET_TEMPLATES:
        if t["name"] == name:
            return {"template": name, "description": t["description"],
                    "time_factor": t["time_factor"], "steps": t["steps"]}
    return None


def strategy_to_config(strategy: dict[str, Any], base: Config,
                       *, source_lang: str = "auto", target_lang: str = "zh") -> Config:
    """把策略 JSON 应用到一份 Config 副本上，返回内核可用的 Config。

    策略可为预设模板名 ({"template": "标准翻译"}) 或自定义 ({"steps": {...}})。
    """
    import copy
    cfg = copy.deepcopy(base)
    cfg.source_lang = source_lang or cfg.source_lang
    cfg.target_lang = target_lang or cfg.target_lang

    if "template" in strategy:
        defn = builtin_template_definition(strategy["template"])
        steps = defn["steps"] if defn else _standard()
    else:
        steps = strategy.get("steps", _standard())

    cfg.pipeline.book_understanding = bool(steps.get("book_understanding", False))
    cfg.pipeline.polish = bool(steps.get("polish", False))
    cfg.punctuation_normalize = bool(steps.get("punctuation_normalize", True))
    cfg.pipeline.review = bool(steps.get("chapter_review", False))
    cfg.pipeline.autofix_severe = bool(steps.get("autofix", False))
    cfg.pipeline.consistency_qa = bool(steps.get("consistency_qa", False))
    bt = steps.get("backtranslate", 0.0)
    try:
        cfg.pipeline.backtranslate_sample = float(bt) if bt else 0.0
    except (TypeError, ValueError):
        cfg.pipeline.backtranslate_sample = 0.0
    # 一致性 QA 在 run_all 里由 do_qa 控制；这里同步到 config 以便默认行为一致。
    return cfg
