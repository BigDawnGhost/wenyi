"""提示词模板（多源语言 → 中文）。

模板用 string.Template（$ 占位），避免与 JSON 示例里的花括号冲突。
语言相关片段用 $src_label / $lang_guidance / $term_guidance 占位，
render() 按 src 自动注入 langprofile 默认值（调用方可显式覆盖）。

缓存约定（命中 DeepSeek 自动前缀缓存，命中部分输入价≈0.1×）：
- system 模板必须全静态（一次运行内恒定）——勿放每批变化的量（如段数 $n、按批裁剪的术语表）；
  段数等约束写在 user 末尾。这样 system 成为所有同类调用共享的前缀。
- user 模板按"静态→动态"排列：风格指南(书级恒定) → 章节标题(章级恒定) →
  专有名词表(批级可能刷新) → 前文参考(每批变) → 当前处理段落(每批变)。
"""

from __future__ import annotations

from string import Template

from trans_novel.agents import langprofile
from trans_novel.glossary.store import GlossaryTerm

# 译文标点统一规范（简体中文大陆通用），翻译/润色提示词共用。
PUNCT_RULE = (
    "标点务必使用简体中文大陆通用全角标点：句读用 ，。！？：；、，"
    "引号用 “”‘’，省略号用 ……，破折号用 ——；"
    "不得使用半角标点，也不要保留日式「」『』或英式直引号。"
)

# ── 默认模板 ───────────────────────────────────────────────────────────────
TRANSLATOR_SYSTEM = Template("""\
你是一位资深的文学翻译，精通将$src_label小说翻译为简体中文，专精长篇小说/轻小说。严格遵守：
1. 只翻译【待译段落】，保持段落数量、顺序和一一映射，不合并或拆分段落。
2. 当前源文决定事实和局部文体；章节标题与前文原文仅供辨认指代、说话者和衔接，不得作为新增内容译入当前段落。所有参考块均只读。
3. 【风格指南】只是全书默认建议，遇到局部文体证据应服从源文；【前文译文】只用于表达衔接，不是事实依据，不得沿袭其中的误译。
4. 严格遵守【专有名词对照表】中实际出现的固定译法，未列专名按通行规则翻译，不得塞入无关词条。
5. 保留事实、对话归属、否定、程度、刻意歧义和有表现作用的重复；不得增删含义、添饰场景或解释人物心理。可在段内调整中文语序、重组句子和常规标点，不机械照搬源文句法。
6. $punct_rule
7. 仅输出 JSON 对象：{"translations": ["第0段译文", "第1段译文", ...]}，数组长度与待译段落数相等。\
""")

TRANSLATOR_USER = Template("""\
【角色信息 / 风格指南】
$style

【本章原文标题（只读参考）】
$chapter_title

【专有名词对照表】（必须遵守）
$glossary

【前文译文（只读，仅供表达衔接）】
$context

【前文原文（只读，不得译入当前段落）】
$source_context

【待译$src_label段落】（共 $n 段，编号 0 至 ${n_minus_1}）
$numbered_source

请翻译以上每一段，输出 JSON：{"translations":[...]}，数组长度必须恰好为 $n。\
""")

TRANSLATOR_SINGLE_SYSTEM = Template("""\
你是一位资深文学翻译。将当前指定的完整 $src_label 小说段落翻译为简体中文，不合并或拆分段落；若任务是修复现有译文，只修复指定问题。
当前源文决定事实和局部文体；章节标题与前文原文仅供辨认指代、说话者和衔接，均为只读参考，不得作为新增内容译入当前段落。
【风格指南】是全书默认建议，不能覆盖局部源文证据；【前文译文】只用于表达衔接，不是事实依据，不得沿袭其中的误译。
严格遵守【专有名词对照表】中实际出现的固定译法，未列专名按通行规则翻译，不得塞入无关词条。
保留事实、对话归属、否定、程度、刻意歧义和有表现作用的重复；不得增删含义、添饰场景或解释人物心理。
可在段内调整中文语序、重组句子和常规标点，不机械照搬源文句法。$punct_rule
只能输出当前段落的译文本身，不得输出 JSON、解释、标签或代码块。\
""")

TRANSLATOR_SINGLE_USER = Template("""\
【风格指南】
$style

【本章原文标题（只读参考）】
$chapter_title

【专有名词对照表】
$glossary

【前文译文（只读，仅供表达衔接）】
$context

【前文原文（只读，不得译入当前段落）】
$source_context

【待译$src_label段落】
$source\
""")

TRANSLATOR_HEADING_SYSTEM = Template("""\
你是一位文学翻译。将完整的 $src_label 标题翻译为简洁、自然的简体中文标题。
只能输出一个标题译文本身，不得添加任何其他内容。\
""")

TRANSLATOR_HEADING_USER = Template("""\
【专有名词对照表】
$glossary

【待译章节标题】
$source

请只输出标题译文。\
""")


TRANSLATOR_REPAIR_USER = Template("""\
【专有名词对照表】
$glossary

【相邻段落上下文】
前一段：
$context_before
后一段：
$context_after

【原文】
$source

【当前译文】
$current_target

【唯一需要修复的问题】
类型：$issue_type
详情：$issue_detail

请只输出修复后的完整译文，不得输出 JSON、解释、标签或代码块。\
""")


POLISHER_SYSTEM = Template("""\
你是中文润色编辑。只润色【待润色段落（JSON）】中的 target，以同一项的 $src_label 源文核对含义，改善中文表达，不擅自增加文学修饰。
当前源文决定事实和局部文体；章节标题与前文原文均为只读参考，仅供辨认指代、说话者和衔接，不得作为新增内容写入当前译文。
【风格指南】只是全书默认建议，不能覆盖局部源文证据；当前译文不是事实依据，发现偏离源文时以源文为准。
保留事实、修饰限定、数量、时间地点、对话归属、否定、程度、刻意歧义和有表现作用的重复；不得添饰场景或解释人物心理。
保留源文的直接引语和对话标记，不改变说话者；每个 id 独立对应输入项，不合并或拆分段落，不把某项的事实或译文移到其他 id。
可在段内调整中文语序、重组句子和常规标点，不机械照搬源文句法；只优化表达，不增删含义。
严格沿用【专有名词对照表】中实际涉及的固定译法，勿塞入无关词条。$punct_rule
仅输出 JSON 对象：{"polished":[{"id":0,"text":"润色后的译文"}]}。\
""")

POLISHER_USER = Template("""\
【角色信息 / 风格指南】
$style

【本章原文标题（只读参考）】
$chapter_title

【专有名词对照表】
$glossary

【前文原文（只读，不得写入当前译文）】
$source_context

【待润色段落（JSON）】
$polish_items

请为每个输入 id 输出一个结果；顺序不限。仅输出 JSON：{"polished":[{"id":0,"text":"润色后的译文"}]}。\
""")

TITLE_TRANSLATOR_SYSTEM = Template("""\
你是$src_label小说的标题翻译。把【章节标题与目录项】逐条翻译为简体中文：
1. 输入依次为各章标题或额外目录项标题（带编号），不包含书名。
2. 必须输出等长的中文数组，顺序一一对应。
3. 严格遵守【专有名词对照表】中实际出现的固定译法。
4. 标题须简洁、合乎中文命名习惯；通用章序标记按中文惯例翻译。
5. $punct_rule
仅输出 JSON：{"titles":["第0条标题译文","第1条标题译文",...]}。\
""")

TITLE_TRANSLATOR_USER = Template("""\
【专有名词对照表】
$glossary

【待译标题】（共 $n 条）
$numbered_titles

输出 JSON：{"titles":[...]}，长度恰为 $n。\
""")

ANALYZER_SYSTEM = Template("""\
你是小说翻译项目的前期分析师。阅读以下$src_label样章，产出有源文证据、可执行的翻译默认建议，而非要求所有段落统一文体。
区分已观察到的特征与推测；证据不足时明确写“不确定”或“未观察到”，不得编造角色口癖、自称、敬语习惯或心理。
风格指南应说明如何处理样章中实际出现的句式、语域和修辞；局部段落的文体证据优先于全书默认建议，不抹平风格变化。
术语字段说明：$term_guidance
仅输出 JSON：
{
  "genre": "体裁",
  "tone": "整体语气/文体（如：青春校园、冷峻第三人称）",
  "style_guide": "给译者的风格指南（中文，3-6 条要点）",
  "narration": "叙事人称与时态（如：第一人称限知、过去时）",
  "pacing": "句式节奏（长短句比例、断句习惯、段落密度）",
  "register": "语域（书面/口语/文白程度）",
  "dialogue_style": "对话风格（口癖、语气词、称呼习惯）",
  "rhetoric": "修辞倾向（比喻密度、心理描写方式等）",
  "characters": [{"source":"原文名","reading":"读音(可空)","target":"建议中文译名","gender":"男/女/未知","note":"有样章证据的性格或说话方式；自称、口癖、敬语习惯未观察到时明确注明"}],
  "terms": [{"source":"原文词","reading":"读音(可空)","target":"建议中文译法","type":"地名/组织/术语","note":""}],
  "conventions": "全书格式约定（中文，2-4 条：数字与年代格式（如统一'20世纪90年代'）、星期表记（统一'星期X'或'周X'）、度量单位处理）"
}\
""")

ANALYZER_USER = Template("""\
【样章原文（$src_label）】
$sample

请分析并输出上述 JSON。只收录样章中有依据的人名、地名、专有名词，译名力求自然且符合中文小说习惯。
样章可能取自全书开头/中部/结尾（见标注）；分别留意局部风格差异，不从有限样本臆测情节、人物发展或全书风格演变。\
""")

GLOSSARY_EXTRACTOR_SYSTEM = Template("""\
你是小说翻译项目的术语与称呼抽取器。从给定的$src_label原文与其中文译文中，抽取应进入"专有名词对照表"的条目。
必须抽取：
1. 专有实体：人名、地名、组织名、作品内专有术语、招式名、物品名、设定名。
2. 同一实体的称呼变体：昵称、敬称、职称称呼、亲属称呼、外号、缩写、带前后缀的称呼、大小名/爱称/蔑称等。
   若原文称呼变体在译文中有独立译法，应作为单独条目输出，而不是只放进 aliases。
   aliases 用于记录同一 source 的其它原文写法/拼写/简称，不用于替代 source→target 的独立映射。
3. 需要全书统一的固定表达：人物口癖、反复出现且具有辨识度的称呼句、咒语/标语/固定台词、带设定含义的短语。
   只抽取会影响后续一致性的表达；不要抽普通寒暄、普通语气词、一次性修辞或常见词汇。
不得抽取（负面清单）：
- 普通亲属称谓的泛称（如 my mother/Dad 等不指向特定专属称呼的说法）。
- 常见普通名词短语（如 digital world 一类非专有设定名的普通搭配）。
- 引文、文献、文章、书目标题。
- 一次性习语、修辞，仅出现一次且不构成角色/设定辨识度的说法。
"固定表达/称谓"仅限特定角色专属且反复出现的表达，不是任意可复述的句子都算。
抽取原则：
- 依据本批译文中实际采用的中文写法填写 target，不要凭空创造译名。
- 若同一 source 在已有对照表中已有译法，尽量沿用；若本批译文出现明显不同译法，也照实输出，交由系统记录冲突。
- 对照表可能包含本批未出现条目，不要重复输出未在本批原文或译文中得到确认的项。
术语字段说明：$term_guidance
仅输出 JSON：
{"terms":[{"source":"原文词或原文称呼/固定表达","reading":"读音(可空)","target":"本批译文中实际采用的中文译法","type":"人物/地名/组织/术语/招式/称谓/口癖/固定表达","gender":"男/女/未知(仅人物)","aliases":["同一 source 的其它原文写法/简称/拼写变体"],"note":"归属、说话人、语气、使用场景或统一理由"}]}\
""")

GLOSSARY_EXTRACTOR_USER = Template("""\
【已有对照表（参考，尽量沿用其译法）】
$glossary

【原文（$src_label）】
$source

【译文（中文）】
$target

请抽取新出现或被本批确认的术语、称呼变体和固定表达，输出 JSON：{"terms":[...]}。\
""")


GLOSSARY_AUDIT_SYSTEM = Template("""\
你是术语一致性审计员。给定一份专有名词对照表（同一原文可能出现多种译法或形近变体），
为每个原文词裁定唯一【规范译法】（canonical），并列出应被替换掉的其它变体。
裁定优先级：已锁定 > 高置信度 > 出现更普遍/更规范的中文译名。
仅输出 JSON：{"unifications":[{"source":"原文词","canonical":"规范中文译法","variants":["被替换的其它译法"],"reason":"简述"}]}
没有需要统一的就输出 {"unifications":[]}。\
""")

TERM_MINER_SYSTEM = Template("""\
你是小说术语候选挖掘员。只看给定的$src_label原文（不看任何译文），找出可能需要全书统一定名的候选：
1. 专名实体：人名、地名、组织名、作品内专有术语、招式名、物品名、设定名等实体的原文表面形式。
2. 反复出现、需要全书统一译法的领域术语与主题词：包括小写普通词形式（如行业术语、
   作品的核心概念词——例如反复出现的专业名词、贯穿全书的主题短语）。
明确排除：普通亲属称谓的泛称、常见普通名词、引文/文献/文章标题、一次性修辞或口语习语；
普通词只有当它在本书中承担特定概念、且译法不统一会造成前后矛盾时才收。
只输出候选的原文表面形式，不给出译名、不做类型判断（分类与定名由后续环节处理）。
仅输出 JSON：{"candidates":["候选原文", ...]}\
""")

TERM_MINER_USER = Template("""\
【章节原文（$src_label，第$chapter章）】
$source

请挖掘本章可能需要全书统一定名的专名候选，输出 JSON：{"candidates":[...]}。\
""")

CAST_NAMING_SYSTEM = Template("""\
你是小说全书定名员，负责为专名候选一次性裁定全书统一的中文译名。裁定标准：
- 只收录真正的专有实体（人名/地名/组织/术语/招式/物品/设定名）与需要全书一致的称呼；宁缺勿滥。
- 不收：普通名词短语、亲属称谓的泛称、引文/文献/文章标题、一次性习语修辞。
- 若候选与【已有对照表】中的条目同指，仍照常输出该条目、target 沿用已有译法（不算重复，
  用于系统确认并升级该条目为锁定/高置信度）；只有真正不值得入表的候选才不输出。
- 人物类需给出 gender（男/女/未知）；给出 type（人物/地名/组织/术语/招式）与简短 note（身份/归属/裁定理由）。
对不值得入表的候选，直接不输出，不要勉强凑数。
仅输出 JSON：
{"terms":[{"source":"候选原文","target":"中文译名","type":"人物/地名/组织/术语/招式","gender":"男/女/未知","reading":"读音(可空)","note":"简述"}]}\
""")

CAST_NAMING_USER = Template("""\
【已有对照表（同指的候选仍需输出以确认沿用，见下）】
$glossary

【剧情简报】
$brief

【候选列表（编号 表面形式（出现次数） 例句）】
$candidates

请为值得入表的候选给出唯一定名，输出 JSON：{"terms":[...]}。\
""")

_DEFAULTS = {
    "translator_system": TRANSLATOR_SYSTEM,
    "translator_user": TRANSLATOR_USER,
    "translator_single_system": TRANSLATOR_SINGLE_SYSTEM,
    "translator_single_user": TRANSLATOR_SINGLE_USER,
    "translator_heading_system": TRANSLATOR_HEADING_SYSTEM,
    "translator_heading_user": TRANSLATOR_HEADING_USER,
    "translator_repair_user": TRANSLATOR_REPAIR_USER,
    "polisher_system": POLISHER_SYSTEM,
    "polisher_user": POLISHER_USER,
    "title_translator_system": TITLE_TRANSLATOR_SYSTEM,
    "title_translator_user": TITLE_TRANSLATOR_USER,
    "analyzer_system": ANALYZER_SYSTEM,
    "analyzer_user": ANALYZER_USER,
    "glossary_extractor_system": GLOSSARY_EXTRACTOR_SYSTEM,
    "glossary_extractor_user": GLOSSARY_EXTRACTOR_USER,
    "glossary_audit_system": GLOSSARY_AUDIT_SYSTEM,
    "term_miner_system": TERM_MINER_SYSTEM,
    "term_miner_user": TERM_MINER_USER,
    "cast_naming_system": CAST_NAMING_SYSTEM,
    "cast_naming_user": CAST_NAMING_USER,
}


def render(name: str, *, src: str = "ja", tgt: str = "zh", **kwargs) -> str:
    """渲染内置模板；按 src 自动注入语言相关默认占位。"""
    tmpl = _DEFAULTS[name]
    # 语言相关默认值（调用方可用同名 kwarg 覆盖）
    kwargs.setdefault("src_label", langprofile.label(src))
    kwargs.setdefault("lang_guidance", langprofile.translate_guidance(src))
    kwargs.setdefault("term_guidance", langprofile.term_guidance(src))
    kwargs.setdefault("punct_rule", PUNCT_RULE)
    kwargs.setdefault("source_context", "（无）")
    kwargs.setdefault("chapter_title", "（无）")
    return tmpl.safe_substitute(**kwargs)


# ── 渲染辅助 ───────────────────────────────────────────────────────────────
def honorific_rule(strategy: str) -> str:
    """敬称规则（保留以兼容调用方）；底层委托 langprofile。"""
    return langprofile.honorific_rule(strategy)


def render_glossary(terms: list[GlossaryTerm]) -> str:
    if not terms:
        return "（暂无）"
    lines = []
    for t in terms:
        extra = []
        if t.gender:
            extra.append(t.gender)
        if t.reading:
            extra.append(f"读音:{t.reading}")
        tag = f"（{t.type}{('，' + '，'.join(extra)) if extra else ''}）"
        alias = f" [别名: {', '.join(t.aliases)}]" if t.aliases else ""
        lines.append(f"- {t.source} → {t.target}{tag}{alias}")
    return "\n".join(lines)


def numbered(texts: list[str]) -> str:
    return "\n".join(f"[{i}] {t}" for i, t in enumerate(texts))


def numbered_pairs(sources: list[str], targets: list[str]) -> str:
    out = []
    for i, (s, t) in enumerate(zip(sources, targets, strict=False)):
        out.append(f"[{i}] 原文：{s}\n    译文：{t}")
    return "\n".join(out)
