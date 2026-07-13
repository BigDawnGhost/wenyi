"""提示词模板（多源语言 → 中文）。

模板用 string.Template（$ 占位），避免与 JSON 示例里的花括号冲突。
语言相关片段用 $src_label / $lang_guidance / $term_guidance 占位，
render() 按 src 自动注入 langprofile 默认值（调用方可显式覆盖）。

缓存约定（命中 DeepSeek 自动前缀缓存，命中部分输入价≈0.1×）：
- system 模板必须全静态（一次运行内恒定）——勿放每批变化的量（如段数 $n、按批裁剪的术语表）；
  段数等约束写在 user 末尾。这样 system 成为所有同类调用共享的前缀。
- user 模板按"静态→动态"排列：风格指南/全书概览(书级恒定) → 本章梗概(章级恒定) →
  专有名词表(批级可能刷新) → 前文译文(每批变) → 待译正文(每批变)。前缀越长且越稳定，命中越多。
"""

from __future__ import annotations

from string import Template

from ..glossary.store import GlossaryTerm
from . import langprofile

# 译文标点统一规范（简体中文大陆通用），翻译/润色提示词共用。
PUNCT_RULE = (
    "标点务必使用简体中文大陆通用全角标点：句读用 ，。！？：；、，"
    "引号用 “”‘’，省略号用 ……，破折号用 ——；"
    "不得使用半角标点，也不要保留日式「」『』或英式直引号。"
)

# ── 默认模板 ───────────────────────────────────────────────────────────────
TRANSLATOR_SYSTEM = Template("""\
你是一位资深的文学翻译，精通将$src_label小说翻译为简体中文，专精长篇小说/轻小说。严格遵守：
1. 忠实原文，绝不漏译、增译，绝不合并或拆分段落；保留原文分段。
2. 输入是带编号的$src_label段落数组。必须输出等长的中文译文数组（数量与输入段落严格相等），
   顺序、数量与输入严格一一对应；第 i 个译文对应第 i 段原文。
3. 【专有名词对照表】是全书对照表的**相关子集参考**，可能含本批未出现的词条：**只有当某词条原文确实出现在
   本批待译段落里，才套用其固定译法**，切勿把与本批无关的词条硬塞进译文。已列词条全书统一用其译法；
   表中未列的专名，沿用【前文回顾】中已出现的译法，勿另起译名。
4. 参考【全书概览】把握整体走向（主线剧情、人物弧光、伏笔与谜底），使本段措辞与后文不冲突；
   参考【本章梗概】把握本章脉络；参考【前文译文】保持衔接：代词指代、人物称谓、语气与跨段句意须自然连贯。
5. 源语言相关要点：
$lang_guidance
6. 保留原文语气与文体；**严格执行【风格指南】给出的叙事人称、句式节奏与语域**；
   对话按角色的口癖/自称习惯译出辨识度；心理、修辞按中文小说习惯自然表达，不生硬直译、不堆砌翻译腔。
7. $punct_rule
8. 仅输出 JSON 对象：{"translations": ["第0段译文", "第1段译文", ...]}，不要任何解释或思考过程。\
""")

TRANSLATOR_USER = Template("""\
【角色信息 / 风格指南】
$style

【全书概览】
$book_synopsis

【本章梗概】
$chapter_digest

【专有名词对照表】（必须遵守）
$glossary

【前文译文（最近）】
$context

【待译$src_label段落】（共 $n 段，编号 0 至 ${n_minus_1}）
$numbered_source

请翻译以上每一段，输出 JSON：{"translations":[...]}，数组长度必须恰好为 $n。\
""")

TRANSLATOR_FIX_USER = Template("""\
【角色信息 / 风格指南】
$style

【全书概览】
$book_synopsis

【本章梗概】
$chapter_digest

【专有名词对照表】（必须遵守）
$glossary

【前文译文】
$context_before

【后文译文】
$context_after

【审校意见】（首译存在的问题，重译必须修正）
$feedback

【待重译$src_label段落】（仅 1 段）
[0] $source

请重译该段，完整传达原文全部信息并与前后文衔接，输出 JSON：{"translations":["译文"]}，数组长度恰为 1。\
""")

REVIEWER_SYSTEM = Template("""\
你是严格的译文审校，比对$src_label原文与中文译文，逐段找出**确凿**的问题。问题类型：
- missing：漏译（原文有的信息译文缺失）
- added：增译（译文凭空增加原文没有的信息）
- mistranslation：误译/误读原意
- terminology：原文确实出现、且对照表已给固定译法的词，译文未遵守
  （对照表为全书参考，含本批未出现的词条；只就本批原文实际出现的词判断，勿因表中无关词条误报）
- pronoun：人称/性别代词错误
只报实质性错误：合理的语序调整、自然意译、风格润色**不算问题**，不要报。
拿不准是否为错就不报，宁缺毋滥。每条须给出可直接采纳的 suggestion。仅输出 JSON：
{"issues":[{"index":整数段号,"type":"...","detail":"简述","suggestion":"修改后的译文或具体改法"}]}
没有问题则输出 {"issues":[]}。\
""")

REVIEWER_USER = Template("""\
【专有名词对照表】
$glossary

【逐段对照】（共 $n 段）
$pairs

请审校并输出 JSON：{"issues":[...]}。\
""")

POLISHER_SYSTEM = Template("""\
你是中文润色编辑。在不改变原意、不增删信息的前提下，提升译文的中文流畅度与文学性：
理顺语序、修正翻译腔、统一文体语气。务必保持段数不变、与输入一一对应。
严格沿用【专有名词对照表】的固定译法（表为全书参考，仅就译文实际涉及的词沿用，勿塞入无关词条）。$punct_rule
仅输出 JSON：{"polished":["第0段","第1段",...]}，长度与输入段数相等。\
""")

POLISHER_USER = Template("""\
【角色信息 / 风格指南】
$style

【专有名词对照表】
$glossary

【待润色中文译文】（共 $n 段）
$numbered_target

输出 JSON：{"polished":[...]}，长度恰为 $n。\
""")

TITLE_TRANSLATOR_SYSTEM = Template("""\
你是$src_label小说的标题翻译。把【章节标题与目录项】逐条翻译为简体中文：
1. 输入依次为各章标题或额外目录项标题（带编号），不包含书名。
2. 必须输出等长的中文数组（数量与输入条数严格相等），顺序一一对应。
3. 严格遵守【专有名词对照表】的固定译法（人名/地名/术语全书一致）。
4. 标题须简洁、合乎中文书名/章节命名习惯；不加引号、书名号或解释；
   形如「第3章」「序章」「エピローグ」之类的卷章序号/通用标记，按中文惯例翻译
   （如「第3章」「序章」「尾声」），不要音译。
5. $punct_rule
仅输出 JSON：{"titles":["第0条标题译文","第1条标题译文",...]}，长度与输入条数相等。\
""")

TITLE_TRANSLATOR_USER = Template("""\
【专有名词对照表】
$glossary

【待译标题】（共 $n 条）
$numbered_titles

输出 JSON：{"titles":[...]}，长度恰为 $n。\
""")

ANALYZER_SYSTEM = Template("""\
你是小说翻译项目的前期分析师。阅读以下$src_label样章，产出供后续翻译统一遵循的基准信息。
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
  "characters": [{"source":"原文名","reading":"读音(可空)","target":"建议中文译名","gender":"男/女/未知","note":"性格/语气特征，须包含说话方式：自称、口癖、敬语习惯"}],
  "terms": [{"source":"原文词","reading":"读音(可空)","target":"建议中文译法","type":"地名/组织/术语","note":""}]
}\
""")

ANALYZER_USER = Template("""\
【样章原文（$src_label）】
$sample

请分析并输出上述 JSON。人名、地名、专有名词尽量找全，译名力求自然且符合中文小说习惯。
样章可能取自全书开头/中部/结尾（见标注），请综合判断整体风格及其演变。\
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

BACKTRANSLATE_SYSTEM = Template("""\
你是回译译者。把给定的中文译文回译成$src_label，只看中文、忠实表达其含义，输出 JSON：
{"backtranslations":["...",...]}，长度与输入一致。\
""")

BACKTRANSLATE_USER = Template("""\
【中文译文】（共 $n 段）
$numbered_target

输出 JSON：{"backtranslations":[...]}。\
""")

CONSISTENCY_SYSTEM = Template("""\
你是全书一致性审查员。给定专有名词对照表和若干章节译文摘要，检查：
术语译法是否前后统一、同一人物代词性别是否一致、语气文体是否漂移、标点是否统一为简体中文规范。
仅输出 JSON：{"issues":[{"type":"terminology/pronoun/tone/punctuation","detail":"...","where":"章节线索"}]}。\
""")

CHAPTER_DIGEST_SYSTEM = Template("""\
你是小说章节梗概员。阅读给定的$src_label单章原文，用简体中文写出该章梗概（不超过 200 字）：
交代本章关键情节推进、登场人物及其处境、重要信息或转折，去除细枝末节。只输出梗概正文，不要解释。\
""")

CHAPTER_DIGEST_USER = Template("""\
【章节原文（$src_label）】
$source

请输出该章中文梗概（不超过 200 字）。\
""")

BOOK_SYNOPSIS_SYSTEM = Template("""\
你是小说全书概览员。依据【前期分析】与【各章梗概】，用简体中文写出一份"全书概览"（不超过 500 字），
供译者在翻译任意章节前把握全局，避免与后文冲突：
主线剧情走向与结局、主要人物及其关系与弧光、核心设定/谜底/重要伏笔、整体基调。
只输出概览正文，不要解释或分点编号。\
""")

BOOK_SYNOPSIS_USER = Template("""\
【前期分析】
$analysis

【各章梗概】
$digests

请综合以上，输出全书概览（不超过 500 字）。\
""")

_DEFAULTS = {
    "translator_system": TRANSLATOR_SYSTEM,
    "translator_user": TRANSLATOR_USER,
    "translator_fix_user": TRANSLATOR_FIX_USER,
    "reviewer_system": REVIEWER_SYSTEM,
    "reviewer_user": REVIEWER_USER,
    "polisher_system": POLISHER_SYSTEM,
    "polisher_user": POLISHER_USER,
    "title_translator_system": TITLE_TRANSLATOR_SYSTEM,
    "title_translator_user": TITLE_TRANSLATOR_USER,
    "analyzer_system": ANALYZER_SYSTEM,
    "analyzer_user": ANALYZER_USER,
    "glossary_extractor_system": GLOSSARY_EXTRACTOR_SYSTEM,
    "glossary_extractor_user": GLOSSARY_EXTRACTOR_USER,
    "backtranslate_system": BACKTRANSLATE_SYSTEM,
    "backtranslate_user": BACKTRANSLATE_USER,
    "consistency_system": CONSISTENCY_SYSTEM,
    "chapter_digest_system": CHAPTER_DIGEST_SYSTEM,
    "chapter_digest_user": CHAPTER_DIGEST_USER,
    "book_synopsis_system": BOOK_SYNOPSIS_SYSTEM,
    "book_synopsis_user": BOOK_SYNOPSIS_USER,
}

def render(name: str, *, src: str = "ja", tgt: str = "zh", **kwargs) -> str:
    """渲染内置模板；按 src 自动注入语言相关默认占位。"""
    tmpl = _DEFAULTS[name]
    # 语言相关默认值（调用方可用同名 kwarg 覆盖）
    kwargs.setdefault("src_label", langprofile.label(src))
    kwargs.setdefault("lang_guidance", langprofile.translate_guidance(src))
    kwargs.setdefault("term_guidance", langprofile.term_guidance(src))
    kwargs.setdefault("punct_rule", PUNCT_RULE)
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
    for i, (s, t) in enumerate(zip(sources, targets)):
        out.append(f"[{i}] 原文：{s}\n    译文：{t}")
    return "\n".join(out)
