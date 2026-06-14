"""提示词模板（多源语言 → 中文）。

模板用 string.Template（$ 占位），避免与 JSON 示例里的花括号冲突。
语言相关片段用 $src_label / $lang_guidance / $term_guidance 占位，
render() 按 src 自动注入 langprofile 默认值（调用方可显式覆盖）。
若仓库根存在 prompts/{name}.{src}-{tgt}.md，则优先用该文件覆盖默认模板。
"""

from __future__ import annotations

import os
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
2. 输入是带编号的$src_label段落数组（共 $n 段）。必须输出等长（恰好 $n 个）的中文译文数组，
   顺序、数量与输入严格一一对应；第 i 个译文对应第 i 段原文。
3. 严格遵守【专有名词对照表】给定的固定译法（人名/地名/术语/称谓），全书一致；
   表中未列的专名，沿用【前文回顾】中已出现的译法，勿另起译名。
4. 参考【前文回顾】保持上下文连贯：代词指代、人物称谓、语气与跨段句意须与前文自然衔接。
5. 源语言相关要点：
$lang_guidance
6. 保留原文语气与文体；对话、心理、修辞按中文小说习惯自然表达，不生硬直译、不堆砌翻译腔。
7. $punct_rule
8. 仅输出 JSON 对象：{"translations": ["第0段译文", "第1段译文", ...]}，不要任何解释或思考过程。\
""")

TRANSLATOR_USER = Template("""\
【专有名词对照表】（必须遵守）
$glossary

【角色信息 / 风格指南】
$style

【前文回顾】
$context

【待译$src_label段落】（共 $n 段，编号 0 至 ${n_minus_1}）
$numbered_source

请翻译以上每一段，输出 JSON：{"translations":[...]}，数组长度必须恰好为 $n。\
""")

REVIEWER_SYSTEM = Template("""\
你是严格的译文审校，比对$src_label原文与中文译文，逐段找出**确凿**的问题。问题类型：
- missing：漏译（原文有的信息译文缺失）
- added：增译（译文凭空增加原文没有的信息）
- mistranslation：误译/误读原意
- terminology：未遵守给定的专有名词对照表
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
严格沿用【专有名词对照表】的固定译法。$punct_rule
仅输出 JSON：{"polished":["第0段","第1段",...]}，长度恰为 $n。\
""")

POLISHER_USER = Template("""\
【专有名词对照表】
$glossary

【角色信息 / 风格指南】
$style

【待润色中文译文】（共 $n 段）
$numbered_target

输出 JSON：{"polished":[...]}，长度恰为 $n。\
""")

TITLE_TRANSLATOR_SYSTEM = Template("""\
你是$src_label小说的标题翻译。把【书名与章节标题】逐条翻译为简体中文：
1. 第 0 条是书名，其余依次为各章标题；输入共 $n 条（带编号）。
2. 必须输出等长（恰好 $n 条）的中文数组，顺序一一对应。
3. 严格遵守【专有名词对照表】的固定译法（人名/地名/术语全书一致）。
4. 标题须简洁、合乎中文书名/章节命名习惯；不加引号、书名号或解释；
   形如「第3章」「序章」「エピローグ」之类的卷章序号/通用标记，按中文惯例翻译
   （如「第3章」「序章」「尾声」），不要音译。
5. $punct_rule
仅输出 JSON：{"titles":["书名译文","第1条标题译文",...]}，长度恰为 $n。\
""")

TITLE_TRANSLATOR_USER = Template("""\
【专有名词对照表】
$glossary

【待译标题】（共 $n 条，第 0 条为书名）
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
  "characters": [{"source":"原文名","reading":"读音(可空)","target":"建议中文译名","gender":"男/女/未知","note":"性格/语气特征"}],
  "terms": [{"source":"原文词","reading":"读音(可空)","target":"建议中文译法","type":"地名/组织/术语","note":""}]
}\
""")

ANALYZER_USER = Template("""\
【样章原文（$src_label）】
$sample

请分析并输出上述 JSON。人名、地名、专有名词尽量找全，译名力求自然且符合中文小说习惯。\
""")

GLOSSARY_EXTRACTOR_SYSTEM = Template("""\
你是术语抽取器。从给定的$src_label原文与其中文译文中，抽取应进入"专有名词对照表"的条目：
人名、地名、组织、专有术语、招式名、需统一处理的称谓。普通词汇不要抽。
对每个条目，依据译文给出实际采用的中文译法。术语字段说明：$term_guidance
仅输出 JSON：
{"terms":[{"source":"原文","reading":"读音(可空)","target":"中文译法","type":"人物/地名/组织/术语/招式/称谓","gender":"男/女/未知(仅人物)","aliases":["别名/变体形式"],"note":""}]}\
""")

GLOSSARY_EXTRACTOR_USER = Template("""\
【已有对照表（参考，尽量沿用其译法）】
$glossary

【原文（$src_label）】
$source

【译文（中文）】
$target

请抽取新出现或被本章确认的专有名词，输出 JSON：{"terms":[...]}。\
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

CONSISTENCY_FIX_SYSTEM = Template("""\
你是全书一致性修订员。依据【专有名词对照表】与各章译文摘要，找出**可安全机械修复的术语/译名不一致**，
给出确定的全局替换（把不统一/错误的中文写法替换为规范写法）。
只处理能安全全局替换的专名/术语；**不要改动代词、语气、句式**（那些交由人工）。
仅输出 JSON：{"replacements":[{"wrong":"被替换写法","right":"规范写法","reason":"简述"}]}，无则 {"replacements":[]}。\
""")

GLOSSARY_AUDIT_SYSTEM = Template("""\
你是术语一致性审计员。给定一份专有名词对照表（同一原文可能出现多种译法或形近变体），
为每个原文词裁定唯一【规范译法】（canonical），并列出应被替换掉的其它变体。
裁定优先级：已锁定 > 高置信度 > 出现更普遍/更规范的中文译名。
仅输出 JSON：{"unifications":[{"source":"原文词","canonical":"规范中文译法","variants":["被替换的其它译法"],"reason":"简述"}]}
没有需要统一的就输出 {"unifications":[]}。\
""")

_DEFAULTS = {
    "translator_system": TRANSLATOR_SYSTEM,
    "translator_user": TRANSLATOR_USER,
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
    "consistency_fix_system": CONSISTENCY_FIX_SYSTEM,
    "glossary_audit_system": GLOSSARY_AUDIT_SYSTEM,
}

_PROMPTS_DIR = os.environ.get("TRANS_NOVEL_PROMPTS_DIR", "prompts")


def render(name: str, *, src: str = "ja", tgt: str = "zh", **kwargs) -> str:
    """渲染模板；按 src 自动注入语言相关默认占位；prompts/{name}.{src}-{tgt}.md 可覆盖。"""
    override = os.path.join(_PROMPTS_DIR, f"{name}.{src}-{tgt}.md")
    if os.path.isfile(override):
        with open(override, "r", encoding="utf-8") as f:
            tmpl = Template(f.read())
    else:
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
