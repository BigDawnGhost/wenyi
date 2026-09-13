"""节点输入指纹的纯函数公式：planner 对账与节点记录共用同一套。

指纹只覆盖影响节点的稳定输入，排除翻译期持续增长的术语上下文及批次预算。
换模型续跑必须失效对应节点及其后代；原文保留章只消费语义决策与源结构。
"""

from __future__ import annotations

import json

from trans_novel.model_profiles import parse_provider_model
from trans_novel.pipeline.state import input_fingerprint, normalize_lang_code
from trans_novel.pipeline.state.frozen import frozen_input_fingerprint  # noqa: F401
from trans_novel.pipeline.state.models import TRANSLATION_POLICY_VERSION


def translator_model_profile(config) -> str:
    """正文翻译模型候选。"""
    return _role_profile(config, "translator")


def translation_model_profile(config) -> str:
    """正文翻译模型候选；单段模式还消费标题分析模型。"""
    roles = (
        ("translator", "analyst") if config.pipeline.single_segment_translation else ("translator",)
    )
    return _role_profile(config, *roles)


def analyst_model_profile(config) -> str:
    """分析、定名与标题模型候选。"""
    return _role_profile(config, "analyst")


def editor_model_profile(config) -> str:
    """润色模型候选。"""
    return _role_profile(config, "editor")


def polish_model_profile(config) -> str:
    """润色模型候选。"""
    return _role_profile(config, "editor")


def fast_model_profile(config) -> str:
    """预扫模型候选。"""
    return _role_profile(config, "fast")


def mine_terms_input_fingerprint(
    chapter_texts: list[str], src_lang: str, concurrency: int, model: str = ""
) -> str:
    return input_fingerprint(chapter_texts, src_lang, concurrency, model)


def _role_profile(config, *roles: str) -> str:
    llm = getattr(config, "llm", None)
    if llm is None:
        return ""
    models = getattr(llm, "models", None)
    if models is None:
        return ""
    profile = {role: list(getattr(models, role, ()) or ()) for role in roles}
    candidates = [candidate for role in roles for candidate in profile[role]]
    if any(parse_provider_model(candidate)[0] == "openai-compatible" for candidate in candidates):
        profile["base_url"] = str(getattr(llm, "base_url", "") or "")
    return json.dumps(profile, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def glossary_semantic_fingerprint_part(terms) -> str:
    """一致性 QA 消费的术语语义稳定序列化（source/target/aliases/type/lock/status）。

    `tools glossary resolve|lock|audit` 只改既有词条的语义字段而不新增 source 时，
    QA 指纹必须随之变化（重新扫描），不能只按 source 拼写判断。
    """
    parts: list[str] = []
    for t in terms or []:
        parts.append(
            "|".join(
                [
                    str(getattr(t, "source", "") or ""),
                    str(getattr(t, "target", "") or ""),
                    ",".join(str(a) for a in (getattr(t, "aliases", None) or [])),
                    str(getattr(t, "type", "") or ""),
                    "1" if getattr(t, "locked", False) else "0",
                    str(getattr(t, "status", "") or ""),
                ]
            )
        )
    return "\n".join(sorted(parts))


def translation_structure_fingerprint_part(segments) -> str:
    """Serialize EPUB slot geometry consumed after plain-text translation."""
    return json.dumps(
        [
            {
                "index": getattr(segment, "index", None),
                "slot_contract": getattr(
                    getattr(segment, "epub_state", None), "slot_contract_sha256", None
                ),
            }
            for segment in segments
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def prepare_input_fingerprint(source_sha: str, src_lang: str, tgt_lang: str) -> str:
    """准备节点：源文件字节哈希 + 解析后的语言对（即运行身份的输入面）。"""
    return input_fingerprint(
        source_sha, normalize_lang_code(src_lang), normalize_lang_code(tgt_lang)
    )


def analyze_input_fingerprint(sample: str, model: str = "") -> str:
    """风格/角色分析：策略版本 + 样章文本 + 模型路由。"""
    return input_fingerprint(TRANSLATION_POLICY_VERSION, sample, model)


def name_terms_input_fingerprint(
    mine_fingerprint: str,
    style_brief: str,
    concurrency: int,
    model: str = "",
) -> str:
    """定名节点：稳定的术语挖掘输入 + 风格/并发/模型配置。"""
    return input_fingerprint(mine_fingerprint, style_brief, concurrency, model)


def preserve_source_input_fingerprint(source_text: str, processing) -> str:
    """原文保留章：语义决策 + 源文与 EPUB 槽位结构。"""
    return input_fingerprint(TRANSLATION_POLICY_VERSION, source_text, processing)


def translate_input_fingerprint(
    source_text: str,
    src_lang: str,
    tgt_lang: str,
    *,
    style_brief: str,
    punctuation_normalize: bool,
    honorific_strategy: str,
    glossary_scope: str,
    single_segment_translation: bool,
    model: str = "",
    processing=None,
) -> str:
    return input_fingerprint(
        TRANSLATION_POLICY_VERSION,
        source_text,
        normalize_lang_code(src_lang),
        normalize_lang_code(tgt_lang),
        style_brief,
        punctuation_normalize,
        honorific_strategy,
        glossary_scope,
        single_segment_translation,
        model,
        processing,
    )


def polish_input_fingerprint(
    source_text: str,
    src_lang: str,
    style_brief: str,
    *,
    punctuation_normalize: bool,
    model: str = "",
) -> str:
    """润色：源文 + 风格 + 标点配置 + 模型（译文本体由 translate 级联驱动）。"""
    return input_fingerprint(
        TRANSLATION_POLICY_VERSION,
        source_text,
        normalize_lang_code(src_lang),
        style_brief,
        punctuation_normalize,
        model,
    )


def titles_input_fingerprint(
    titles: list[str], src_lang: str, tgt_lang: str, model: str = ""
) -> str:
    return input_fingerprint(
        titles, normalize_lang_code(src_lang), normalize_lang_code(tgt_lang), model
    )


def deterministic_qa_input_fingerprint(targets_text: str, glossary_semantic: str) -> str:
    return input_fingerprint(targets_text, glossary_semantic)


def report_input_fingerprint(
    lint_issues: list[dict],
    deterministic_issues: list[dict],
    glossary_terms: list[str],
    titles: list[str],
) -> str:
    return input_fingerprint(lint_issues, deterministic_issues, glossary_terms, titles)


def assemble_input_fingerprint(
    targets_text: str,
    *,
    mono: bool,
    bilingual: bool,
    out_format: str,
    bilingual_order: str,
    output_digest: str | None = None,
) -> str:
    return input_fingerprint(
        targets_text, mono, bilingual, out_format, bilingual_order, output_digest
    )
