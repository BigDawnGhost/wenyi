from __future__ import annotations

from string import Template
from ..glossary.store import GlossaryTerm
from . import langprofile

PUNCT_RULE = (
    "标点务必使用简体中文大陆通用全角标点：句读用 ，。！？：；、，"
    "引号用 “”‘’，省略号用 ……，破折号用 ——；"
    "不得使用半角标点，也不要保留日式「」『』或英式直引号。"
)

TRANSLATOR_SYSTEM = Template("""\
你是资深的文学翻译。严格遵循：
1. 忠实原文分段，输出等长中文数组（数量与输入段落严格相等），对应关系一致。
2. 遵守 `<glossary>` 中的译法。表中未列专名，沿用 `<context>` 中的译法。
3. 对话符合人物人设口癖，叙事符合 `<style>`。
4. $punct_rule

Few-Shot 示例：
输入段落：
[0] "Where is he?" she asked.
[1] "I don't know." he replied.

输出格式：
{"translations": [
  "“他在哪里？”她问。",
  "“我不知道。”他回答。"
]}

仅输出 JSON，不要任何附加文字。\
""")

TRANSLATOR_USER = Template("""\
<style>
$style
</style>

<book_synopsis>
【全书概览】
$book_synopsis
</book_synopsis>

<glossary>
$glossary
</glossary>

<chapter_digest>
【本章梗概】
$chapter_digest
</chapter_digest>

<context>
$context
</context>

<source_paragraphs n="$n">
$numbered_source
</source_paragraphs>

<final_instruction>
请翻译以上每段原文。必须且仅输出 JSON 格式 {"translations": [...]}，包含的译文段落数量必须恰好为 $n。禁止任何附加解释。
</final_instruction>\
""")

TRANSLATOR_FIX_USER = Template("""\
<style>
$style
</style>

<book_synopsis>
【全书概览】
$book_synopsis
</book_synopsis>

<glossary>
$glossary
</glossary>

<chapter_digest>
【本章梗概】
$chapter_digest
</chapter_digest>

<context_before>
$context_before
</context_before>

<context_after>
$context_after
</context_after>

<review_feedback>
【审校意见】
$feedback
</review_feedback>

<source_paragraph>
[0] $source
</source_paragraph>

<final_instruction>
依据 <review_feedback> 修订重译。必须且仅输出 JSON 格式 {"translations": ["译文"]}，包含的译文段落数量恰为 1。
</final_instruction>\
""")

REVIEWER_SYSTEM = Template("""\
你是严格的译文审校。比对原文与中文译文，找出确凿的问题：
- missing：漏译
- added：增译
- mistranslation：误译/误读
- terminology：未遵守对照表固定译法
- pronoun：代词性别/人称错误
宁缺毋滥，合理意译或润色不报。给出可直接采纳的 suggestion。

Few-Shot 示例：
输入对照：
[0] 原文：Wait, who are you?
    译文：等待，你是谁？
[1] 原文：I'm Alice.
    译文：我是艾丽。

输出格式：
{"issues": [
  {"index": 0, "type": "mistranslation", "detail": "“Wait”在此为口语叹词，误译为动词“等待”", "suggestion": "“等等，你是谁？”"},
  {"index": 1, "type": "terminology", "detail": "未遵守对照表把 Alice 译为“爱丽丝”", "suggestion": "“我是爱丽丝。”"}
]}

仅输出 JSON，若无问题输出 {"issues": []}。\
""")

REVIEWER_USER = Template("""\
<glossary>
$glossary
</glossary>

<chapter_pairs n="$n">
$pairs
</chapter_pairs>

<final_instruction>
审校以上 <chapter_pairs> 并输出 JSON：{"issues":[...]}。\
</final_instruction>\
""")

POLISHER_SYSTEM = Template("""\
你是中文润色编辑。在不改变原意、不增删信息的前提下，提升中文流畅度与文学性：理顺语序、消除翻译腔。保持段数不变，沿用对照表译法。$punct_rule
参考 `<context>` 确保人称代词、语气与句意衔接自然连贯。

Few-Shot 示例：
输入段落：
[0] 他说他有一只狗。

输出格式：
{"polished": [
  "他说自己养了一只狗。"
]}

仅输出 JSON，不要任何附加解释。\
""")

POLISHER_USER = Template("""\
<style>
$style
</style>

<glossary>
$glossary
</glossary>

<context>
$context
</context>

<target_paragraphs n="$n">
$numbered_target
</target_paragraphs>

<final_instruction>
润色以上 <target_paragraphs>，输出 JSON：{"polished":[...]}，长度恰为 $n。
</final_instruction>\
""")

TITLE_TRANSLATOR_SYSTEM = Template("""\
你是小说标题翻译。翻译章节名或目录标题，保持条数一致。遵守对照表，简洁符合中文命名习惯。$punct_rule

Few-Shot 示例：
输入标题：
[0] Prologue
[1] Chapter 1: The Beginning

输出格式：
{"titles": [
  "序章",
  "第一章：开始"
]}

仅输出 JSON。\
""")

TITLE_TRANSLATOR_USER = Template("""\
<glossary>
$glossary
</glossary>

<titles n="$n">
$numbered_titles
</titles>

<final_instruction>
翻译以上标题，输出 JSON：{"titles":[...]}，长度恰为 $n。
</final_instruction>\
""")

ANALYZER_SYSTEM = Template("""\
你是小说前期分析师。阅读样章，提取风格和人设基准。
术语字段说明：$term_guidance
仅输出 JSON 格式：
{
  "genre": "体裁",
  "tone": "整体基调",
  "style_guide": "风格指南",
  "narration": "叙事人称与时态",
  "pacing": "句式节奏",
  "register": "语域",
  "dialogue_style": "对话习惯",
  "rhetoric": "修辞特征",
  "characters": [{"source":"原文名","reading":"读音","target":"中文译名","gender":"男/女/未知","note":"说话方式与特征"}],
  "terms": [{"source":"原文词","reading":"读音","target":"建议译法","type":"类型","note":""}]
}\
""")

ANALYZER_USER = Template("""\
<sample>
$sample
</sample>

<final_instruction>
分析以上 <sample>，输出上述格式的 JSON。人名、专名尽量找全。
</final_instruction>\
""")

GLOSSARY_EXTRACTOR_SYSTEM = Template("""\
你是术语与称呼抽取器。比对原文与译文，提取专有名词、称呼变体和固定表达。
术语字段说明：$term_guidance
仅输出 JSON 格式：
{"terms":[{"source":"原文词","reading":"读音","target":"中文译法","type":"人物/地名/术语/称谓/口癖/固定表达","gender":"男/女/未知","aliases":["其它拼写/简称"],"note":"归属或统一理由"}]}\
""")

GLOSSARY_EXTRACTOR_USER = Template("""\
<glossary>
$glossary
</glossary>

<source_text>
$source
</source_text>

<target_text>
$target
</target_text>

<final_instruction>
提取新术语/变体，输出 JSON：{"terms":[...]}。
</final_instruction>\
""")

BACKTRANSLATE_SYSTEM = Template("""\
你是回译译者。把给定的中文译文回译成$src_label，仅输出 JSON：
{"backtranslations":["...",...]}，长度与输入一致。\
""")

BACKTRANSLATE_USER = Template("""\
<target_paragraphs n="$n">
$numbered_target
</target_paragraphs>

<final_instruction>
回译以上段落，输出 JSON：{"backtranslations":[...]}。
</final_instruction>\
""")

CONSISTENCY_SYSTEM = Template("""\
你是全书一致性审查员。审查专有名词及代词等一致性问题。
仅输出 JSON：{"issues":[{"type":"terminology/pronoun/tone/punctuation","detail":"描述","where":"章节线索"}]}。\
""")

CONSISTENCY_FIX_SYSTEM = Template("""\
你是全书一致性修订员。依据对照表与摘要找出可安全机械修复的译名不一致。
仅输出 JSON：{"replacements":[{"wrong":"被替换写法","right":"规范写法","reason":"简述"}]}，无则 {"replacements":[]}。\
""")

GLOSSARY_AUDIT_SYSTEM = Template("""\
你是术语一致性审计员。为每个原文词裁定唯一规范译法。
仅输出 JSON：{"unifications":[{"source":"原文词","canonical":"规范译法","variants":["被替换其它译法"],"reason":"简述"}]}，无则 {"unifications":[]}。\
""")

CHAPTER_DIGEST_SYSTEM = Template("""\
你是小说章节梗概员。写出单章中文梗概（不超过 200 字），只输出正文。\
""")

CHAPTER_DIGEST_USER = Template("""\
<source_chapter>
$source
</source_chapter>

<final_instruction>
输出梗概（不超过 200 字）。
</final_instruction>\
""")

BOOK_SYNOPSIS_SYSTEM = Template("""\
你是全书概览员。综合分析与各章梗概，写出全书概览（不超过 500 字），只输出正文。\
""")

BOOK_SYNOPSIS_USER = Template("""\
<analysis>
$analysis
</analysis>

<chapter_digests>
$digests
</chapter_digests>

<final_instruction>
输出全书概览（不超过 500 字）。
</final_instruction>\
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
    "consistency_fix_system": CONSISTENCY_FIX_SYSTEM,
    "glossary_audit_system": GLOSSARY_AUDIT_SYSTEM,
    "chapter_digest_system": CHAPTER_DIGEST_SYSTEM,
    "chapter_digest_user": CHAPTER_DIGEST_USER,
    "book_synopsis_system": BOOK_SYNOPSIS_SYSTEM,
    "book_synopsis_user": BOOK_SYNOPSIS_USER,
}


def render(name: str, *, src: str = "ja", tgt: str = "zh", **kwargs) -> str:
    tmpl = _DEFAULTS[name]
    kwargs.setdefault("src_label", langprofile.label(src))
    kwargs.setdefault("lang_guidance", langprofile.translate_guidance(src))
    kwargs.setdefault("term_guidance", langprofile.term_guidance(src))
    kwargs.setdefault("punct_rule", PUNCT_RULE)
    return tmpl.safe_substitute(**kwargs)


def honorific_rule(strategy: str) -> str:
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
