"""策略 → Config 映射的单元测试（纯逻辑，不依赖 DB）。"""

from __future__ import annotations

from wenyi_core.config import Config

from wenyi_api.strategies import (PRESET_TEMPLATES, builtin_template_definition,
                                  strategy_to_config)


def _base() -> Config:
    return Config.from_dict({"llm": {"provider": "fake"}})


def test_preset_templates_present():
    names = {t["name"] for t in PRESET_TEMPLATES}
    assert {"快速出稿", "标准翻译", "精翻"} == names


def test_template_to_config_quick_disables_extras():
    cfg = strategy_to_config({"template": "快速出稿"}, _base())
    assert cfg.pipeline.book_understanding is False
    assert cfg.pipeline.polish is False
    assert cfg.pipeline.review is False
    assert cfg.pipeline.backtranslate_sample == 0.0
    # 标点规范化在快速出稿里仍开
    assert cfg.punctuation_normalize is True


def test_template_to_config_standard():
    cfg = strategy_to_config({"template": "标准翻译"}, _base(),
                             source_lang="ja", target_lang="zh")
    assert cfg.pipeline.book_understanding is True
    assert cfg.pipeline.polish is True
    assert cfg.pipeline.review is True
    assert cfg.pipeline.autofix_severe is False
    assert cfg.source_lang == "ja"
    assert cfg.target_lang == "zh"


def test_template_to_config_premium_enables_all():
    cfg = strategy_to_config({"template": "精翻"}, _base())
    assert cfg.pipeline.autofix_severe is True
    assert cfg.pipeline.consistency_qa is True
    assert cfg.pipeline.backtranslate_sample == 0.05


def test_custom_strategy():
    cfg = strategy_to_config(
        {"steps": {"book_understanding": False, "polish": True,
                   "punctuation_normalize": True, "chapter_review": False,
                   "autofix": False, "backtranslate": 0.1, "consistency_qa": True}},
        _base(),
    )
    assert cfg.pipeline.polish is True
    assert cfg.pipeline.review is False
    assert cfg.pipeline.backtranslate_sample == 0.1
    assert cfg.pipeline.consistency_qa is True


def test_builtin_definition_lookup():
    d = builtin_template_definition("标准翻译")
    assert d is not None
    assert d["steps"]["polish"] is True
    assert builtin_template_definition("不存在") is None
