"""简体中文目标语言的模型表达策略。"""

from __future__ import annotations

from ..common.registry import TargetProfile
from ..common.tags import base_language, canonical_tag


_LABELS = {
    "ja": "日文",
    "en": "英文",
    "zh": "中文",
    "ru": "俄文",
    "ko": "韩文",
    "fr": "法文",
    "de": "德文",
    "es": "西班牙文",
    "it": "意大利文",
    "pt": "葡萄牙文",
}


def language_label(tag: str | None) -> str:
    """返回用于中文提示词的语言名称。"""
    canonical = canonical_tag(tag)
    base = base_language(canonical)
    if base in _LABELS:
        label = _LABELS[base]
        return label if canonical == base else f"{label}（{canonical}）"
    return f"{canonical}语言" if canonical else "原文语言"


def target_profile(_target: str | None) -> TargetProfile:
    """返回简体中文的完整文本与标点策略。"""
    return TargetProfile(
        prose_guidance=(
            "使用自然、准确的简体中文文学表达；可按中文语序重组句子，但不得遗漏信息；"
            "避免生硬直译和翻译腔，并保持人物口吻、叙事人称与语域一致。"
        ),
        title_guidance=(
            "标题应简洁并符合中文书名和章节命名习惯；通用卷章标记按中文惯例表达，"
            "不添加书名号、引号、解释或原文没有的信息。"
        ),
        punctuation_guidance=(
            "在不违反当前任务其它明确格式要求的前提下，保留输入文本中标点与符号的结构作用；"
            "除句号、逗号等普通句读可按中文语序调整外，引号、括号、问号、叹号、冒号、分号、"
            "破折号、省略号、间隔号、波浪号、斜杠、星号、音符及其他特殊符号均不得遗漏，"
            "并保持其位置、层级、数量、重复形式和配对关系。"
            "标点转换为简体中文大陆通用全角形式：句读用 ，。！？：；、，"
            "引号用 “”‘’，省略号用 ……，破折号用 ——；"
            "不得使用半角句读，也不要保留日式「」『』或英式直引号。"
        ),
        chapter_digest_limit="200 字",
        book_synopsis_limit="500 字",
    )


def source_guidance(source: str | None) -> str:
    """返回以中文描述的源语言理解规则。"""
    base = base_language(source)
    if base == "ja":
        return (
            "- 结合上下文补全日语经常省略的主语、宾语和逻辑关系，不凭空增加信息。\n"
            "- 根据私/僕/俺/あたし等第一人称、敬语等级和句尾表达识别人物语域与关系。\n"
            "- 拟声拟态词应传达其语义、节奏和情绪功能，不机械照搬字面。\n"
            "- 正确区分日文汉字词在上下文中的含义，不因字形熟悉而误读。"
        )
    if base == "en":
        return (
            "- 准确识别英语时态、情态、指代、否定范围和从句逻辑。\n"
            "- 注意 Mr./Ms./Sir 等称谓承载的身份、距离和语气。\n"
            "- 被动语态、长句和关系从句可按目标语言重组，但不得丢失逻辑关系。"
        )
    return "- 准确识别源文语义、指代、语气与句法关系，不因目标语言重组而遗漏信息。"


def _honorific_rule(strategy: str) -> str:
    """返回日译中所用的敬称处理规则。"""
    return {
        "keep_style": (
            "体现敬称所含的人物关系与语气（如 先輩→前辈、ちゃん→小X）；"
            "根据具体人物关系确定“君”等称呼的译法，确定后同一关系全书沿用。"
        ),
        "normalize": "按统一规则处理敬称，避免同一敬称多种译法。",
        "drop": "在不影响语义和人物关系的前提下省略敬称。",
    }.get(strategy, "体现敬称语气并保持全书统一。")


def pair_guidance(
    source: str | None,
    target: str | None,
    honorific_strategy: str = "keep_style",
) -> str:
    """返回以中文描述的语言对特定规则。"""
    src = base_language(source)
    tgt = base_language(target)
    if src == "ja" and tgt == "zh":
        return (
            "- 敬称：" + _honorific_rule(honorific_strategy) + "\n"
            "- 依据角色信息和上下文正确处理中文第三人称代词及人物口吻。\n"
            "- 日文汉字姓名优先采用作品通行中文译名；无通行译名时保持全书一致。"
        )
    if src == "en" and tgt == "zh":
        return (
            "- Mr./Ms./Sir 等称谓按人物关系和中文语境自然处理，全书统一。\n"
            "- 英文专有名词优先采用通行中文译名；无通行译名时统一音译或意译。\n"
            "- 依据姓名、角色信息和上下文正确处理中文“他/她/它”等代词。"
        )
    return "（无额外语言对规则）"


def term_guidance(source: str | None, target: str | None) -> str:
    """返回以中文描述的术语字段填写规则。"""
    src = base_language(source)
    if src == "ja":
        reading = "reading 填假名读音，用于姓名和专名译法消歧"
    elif src == "zh":
        reading = "reading 可填写拼音或其它有助于目标语言转写的读音"
    else:
        reading = "reading 在读音对目标语言译名没有帮助时可留空"
    return (
        f"{reading}；target、type、gender 与 note 必须使用{language_label(target)}；"
        "type 使用目标语言中的简短类别名，例如人物、地名、组织、术语、招式、称谓、敬称、"
        "口癖、固定表达；gender 仅人物填写，使用男、女或未知，非人物留空。"
        "人物性别和代词信息依角色事实与上下文判断。"
    )


def default_term_type(_target: str | None) -> str:
    """返回模型漏填类别时的中文默认术语类型。"""
    return "术语"


def default_person_type(_target: str | None) -> str:
    """返回模型漏填类别时的中文默认人物类型。"""
    return "人物"
