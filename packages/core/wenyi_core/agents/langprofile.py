"""按源语言提供提示词的语言相关片段。

各 agent 的提示词主体语言无关，差异通过这里的 `$src_label`、`$lang_guidance`、
`$term_guidance` 占位注入。prompts.render() 会按 `src` 自动填入默认值，
调用方（如 translator 需带入敬称策略）可显式覆盖。
"""

from __future__ import annotations

LABELS = {
    "ja": "日文", "en": "英文", "zh": "中文", "ru": "俄文", "ko": "韩文",
    "fr": "法文", "de": "德文", "es": "西班牙文", "it": "意大利文", "pt": "葡萄牙文",
}


def label(src: str) -> str:
    """把 ISO 源语言代码转换为提示词中的中文语言名称。"""
    return LABELS.get(src, f"{src}文" if src else "原文")


def honorific_rule(strategy: str) -> str:
    """返回指定敬称策略对应的提示词约束。"""
    return {
        "keep_style": (
            "体现敬称所含的人物关系与语气（如 先輩→前辈、ちゃん→小X）；"
            "根据具体人物关系确定“君”等称呼的译法，确定后同一关系全书沿用。"
        ),
        "normalize": "按统一规则处理敬称，避免同一敬称多种译法。",
        "drop": "在不影响语义和人物关系的前提下省略敬称。",
    }.get(strategy, "体现敬称语气并保持全书统一。")


def translate_guidance(src: str, honorific_strategy: str = "keep_style") -> str:
    """翻译/润色用：源语言相关的译法要点。"""
    if src == "ja":
        return (
            "- 敬称：" + honorific_rule(honorific_strategy) + "\n"
            "- 依据【角色信息】与第一人称（私/僕/俺/あたし 等）体现的语域，正确选择"
            "“他/她”等代词与说话口吻。\n"
            "- 拟声拟态词按中文小说习惯自然表达，不生硬直译。\n"
            "- 汉字词不等于中文词，按语义译，勿照搬日文汉字写法。"
        )
    if src == "en":
        return (
            "- 英文无敬称体系；Mr./Ms./Sir 等称谓按中文习惯自然处理，全书统一。\n"
            "- 依据人名性别与上下文正确选择“他/她/它”；英文不显性别处须联系上下文判断。\n"
            "- 时态、关系从句、长句按中文表达重组断句；被动语态酌情转主动，避免翻译腔。\n"
            "- 英文专有名词按通行译名规范音译/意译，并沿用对照表，全书统一。"
        )
    return "- 忠实传达原意，符合中文小说表达习惯。"


def term_guidance(src: str) -> str:
    """分析/术语抽取用：reading 字段与性别判断的语言相关说明。"""
    if src == "ja":
        return "reading 填假名读音（用于音译消歧）；人物依语气/第一人称判断性别。"
    if src == "en":
        return "reading 可留空（英文无需读音）；人物依姓名常识与上下文判断性别。"
    return "reading 可留空；人物依上下文判断性别。"
