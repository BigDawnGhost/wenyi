"""按翻译方向提供提示词的语言相关片段。

各 agent 的提示词主体语言无关，差异通过这里的语言标签、翻译指导、
术语规则和标点规则注入。prompts.render() 会按 `src` / `tgt` 自动填入默认值，
调用方（如 translator 需带入敬称策略）可显式覆盖。
"""

from __future__ import annotations

LABELS = {
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


def base_code(language: str) -> str:
    """把 ISO/BCP 47 语言标签规整为用于规则匹配的基础语言代码。"""
    value = (language or "").strip().lower().replace("_", "-")
    aliases = {"cn": "zh", "chinese": "zh", "english": "en"}
    value = aliases.get(value, value)
    return value.split("-", 1)[0]


def label(language: str) -> str:
    """把语言代码转换为提示词中的中文语言名称。"""
    code = base_code(language)
    return LABELS.get(code, f"{code}文" if code else "原文")


def target_label(language: str) -> str:
    """返回目标语言标签；中文目标明确限定为简体中文。"""
    return "简体中文" if base_code(language) == "zh" else label(language)


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


def translate_guidance(
    src: str,
    honorific_strategy: str = "keep_style",
    *,
    tgt: str = "zh",
) -> str:
    """翻译/润色用：当前翻译方向的专属译法要点。"""
    source = base_code(src)
    target = base_code(tgt)
    if source == "zh" and target == "en":
        return (
            "- 使用自然、成熟、适合出版的国际英文；拼写标准（美式/英式）以"
            "【角色信息 / 风格指南】的声明为准，未声明时全书固定选用一种、不得混用。\n"
            "- 叙事默认使用过去时；对话、内心独白与原文明确的倒叙/预叙按其时间关系处理，"
            "时态选用与【前文译文】保持一致。\n"
            "- 先准确理解中文语义再按英文重组句子：在段落数量与顺序不变的前提下，段内句子"
            "允许按英文习惯拆分或合并；结合上下文补足中文省略的主语和代词（补足原文隐含的"
            "主语/代词不算增译），保持叙事视角、语气及人物声音一致。\n"
            "- 代词的性别与单复数以【角色信息】及对照表标注为准，并与【前文译文】已用代词"
            "保持一致；人物性别未确认时，用名字复指或 singular they，不得擅自定为男性或女性。\n"
            "- 称谓（师父/师兄/前辈/大人/公子/姑娘/排行称呼/字与号/谦称自称等）按【角色信息 / "
            "风格指南】声明的策略执行；未声明时按体裁默认：修仙、武侠等东方题材用无声调拼音"
            "称谓（Shifu、Shixiong 等，长幼排行按英文社区惯例），其它题材用自然英文对应表达；"
            "同一称谓一经译定全书统一，并与对照表中该人物主条目的策略一致。\n"
            "- 境界、功法、丹田、灵根等修仙/武侠核心设定词：优先采用权威或通行英译，无通行"
            "译法时保留英文社区惯用拼音（如 qi、dantian），最终以风格指南声明的分类策略为准；"
            "其它题材的文化概念用含义能自明的自然英译。\n"
            "- 成语、俗语、古典表达与文化意象按语境自然转述；不得逐字硬译，也不得擅自添加"
            "原文没有的解释、脚注或背景知识。\n"
            "- 诗词、对联、双关、谐音等不可直译处做创造性转译，保住意象、节奏与人物语气，"
            "在行文内自然补偿，不跳过不译，不加脚注或括号注。\n"
            "- 对话按英文惯例重组：引语与说话标签之间用逗号衔接（\"…,\" he said.），引号用法、"
            "插入语位置与段间换行按英文小说排版习惯处理。\n"
            "- 专名优先采用权威或通行英译；否则人名、地名使用无声调汉语拼音并默认保留中文"
            "姓名顺序。文化术语按英文出版惯例意译或保留拼音，一经确定须全书统一。"
        )
    if target != "zh":
        return f"- 忠实传达原意，使用自然、连贯且符合{label(tgt)}出版习惯的表达。"
    if source == "ja":
        return (
            "- 敬称：" + honorific_rule(honorific_strategy) + "\n"
            "- 依据【角色信息】与第一人称（私/僕/俺/あたし 等）体现的语域，正确选择"
            "“他/她”等代词与说话口吻。\n"
            "- 拟声拟态词按中文小说习惯自然表达，不生硬直译。\n"
            "- 汉字词不等于中文词，按语义译，勿照搬日文汉字写法。"
        )
    if source == "en":
        return (
            "- 英文无敬称体系；Mr./Ms./Sir 等称谓按中文习惯自然处理，全书统一。\n"
            "- 依据人名性别与上下文正确选择“他/她/它”；英文不显性别处须联系上下文判断。\n"
            "- 时态、关系从句、长句按中文表达重组断句；被动语态酌情转主动，避免翻译腔。\n"
            "- 英文专有名词按通行译名规范音译/意译，并沿用对照表，全书统一。"
        )
    return "- 忠实传达原意，符合中文小说表达习惯。"


def translate_example(src: str, tgt: str = "zh") -> str:
    """译者系统提示词的方向专属微型示例块；仅 zh→en 提供，其它方向返回空串。"""
    if base_code(src) == "zh" and base_code(tgt) == "en":
        return (
            "【翻译示例】（仅固定译法基调与格式，示例人物与本书无关，勿照抄内容）\n"
            "原文：他睁开眼，发现自己躺在潮湿的山洞里，一时想不起昨夜发生了什么。\n"
            "译文：He opened his eyes and found himself lying in a damp cave, for a moment "
            "unable to recall what had happened the night before.\n"
            "原文：“师父，弟子这就去办。”陈三躬身说道。\n"
            "译文：\"Shifu, your disciple will see to it at once,\" Chen San said with a bow."
        )
    return ""


def term_guidance(src: str, tgt: str = "zh") -> str:
    """分析/术语抽取用：reading 字段与性别判断的语言相关说明。"""
    source = base_code(src)
    target = base_code(tgt)
    if source == "zh" and target == "en":
        return (
            "reading 填无声调汉语拼音；target 优先采用权威或通行英译，否则人名、地名用"
            "无声调汉语拼音并默认保留中文姓名顺序，文化术语按英文出版惯例意译或保留拼音；"
            "同一人物的称谓变体 target 必须与该人物主条目遵循相同的命名与称谓策略；"
            "人物性别依上下文判断，无法确认则填未知。"
        )
    if source == "ja":
        return "reading 填假名读音（用于音译消歧）；人物依语气/第一人称判断性别。"
    if source == "en":
        return "reading 可留空（英文无需读音）；人物依姓名常识与上下文判断性别。"
    return "reading 可留空；人物依上下文判断性别。"


def analysis_style_guidance(tgt: str) -> str:
    """返回前期分析器应生成的目标语言风格指南说明。"""
    if base_code(tgt) == "en":
        return (
            "给译者的英文写作风格指南（用中文表述，关键策略附英文关键词，且须与本 JSON 中 "
            "characters/terms 的 target 实际译法一致）。前 4 条依次声明：①拼写标准（US 或 UK，"
            "全书一律）；②叙事默认时态（如 simple past）；③称谓策略：修仙/武侠等东方题材用"
            "拼音称谓（Shifu、Shixiong 等）或自然英文称谓，排行称呼与谦称自称如何处理；"
            "④术语策略按类别声明：境界/功法/门派/物品/招式各类用通行英译还是保留拼音。"
            "其后 2-4 条为句式、节奏、语域、对话与修辞要求"
        )
    return "给译者的简体中文写作风格指南（用中文列出 3-6 条句式、节奏、语域、对话与修辞要求）"


def analysis_narration_guidance(tgt: str) -> str:
    """返回前期分析器的目标语言叙事字段说明。"""
    if base_code(tgt) == "en":
        return (
            "叙事人称与时态（如：第三人称限知、叙事默认 simple past、倒叙用 past perfect；"
            "须明确英文叙事时态基准）"
        )
    return "叙事人称、视角与中文表达方式（如：第一人称限知、第三人称全知）"


def pronoun_guidance(tgt: str) -> str:
    """返回审校器针对目标语言代词的检查规则。"""
    if base_code(tgt) == "en":
        return (
            "人称/性别代词错误（译文中 he/she/they 等代词的性别与单复数，须与对照表人物"
            "条目的性别标注相符，且与本批其它段落的既有用法一致；原文性别不明而译文擅自"
            "确定性别的也计入）"
        )
    return (
        "人称/性别代词错误（译文中他/她/它等代词须与人物及上下文相符，并与本批其它段落"
        "的既有用法一致；原文性别不明而译文擅自确定性别的也计入）"
    )


def title_guidance(tgt: str) -> str:
    """返回章节标题的目标语言写作规则。"""
    if base_code(tgt) == "en":
        return (
            "标题须简洁自然，遵循英文小说章节标题和大小写惯例；不加引号或解释。"
            "卷章序号及“序章、尾声”等通用标记按自然英文表达，不得音译。"
        )
    return (
        "标题须简洁、合乎中文书名/章节命名习惯；不加引号、书名号或解释；"
        "卷章序号及“序章、尾声”等通用标记按中文惯例翻译，不得音译。"
    )


def punctuation_rule(tgt: str) -> str:
    """返回目标语言的标点与特殊符号约束。"""
    prefix = (
        "在不违反当前任务其它明确格式要求的前提下，保留输入文本中标点与符号的结构作用；"
        "除普通句读可按目标语言语序调整外，引号、括号、问号、叹号、冒号、分号、破折号、"
        "省略号、间隔号、波浪号、斜杠、星号、音符及其他特殊符号均不得遗漏，并保持其层级、"
        "数量、重复形式和配对关系。"
    )
    if base_code(tgt) == "en":
        return (
            prefix + "标点须转换为规范英文形式：使用半角句读，人物对话使用成对的英文双引号、"
            "引语内引语使用单引号，省略号和破折号采用一致的英文排印形式；不得保留中文全角"
            "句读、书名号或日式引号。"
        )
    return (
        prefix + "标点务必转换为简体中文大陆通用全角形式：句读用 ，。！？：；、，"
        "引号用 “”‘’，省略号用 ……，破折号用 ——；不得使用半角标点，也不要保留"
        "日式「」『』或英式直引号。"
    )
