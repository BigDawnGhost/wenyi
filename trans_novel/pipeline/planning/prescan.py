"""Fingerprint inputs assembled from the current run state."""

from __future__ import annotations

from trans_novel.config import Config
from trans_novel.ingest import Chapter, Document, preserved_toc_entry_ids
from trans_novel.pipeline.planning.fingerprints import (
    analyst_model_profile,
    analyze_input_fingerprint,
    assemble_input_fingerprint,
    deterministic_qa_input_fingerprint,
    fast_model_profile,
    glossary_semantic_fingerprint_part,
    mine_terms_input_fingerprint,
    name_terms_input_fingerprint,
    polish_input_fingerprint,
    polish_model_profile,
    prepare_input_fingerprint,
    preserve_source_input_fingerprint,
    report_input_fingerprint,
    titles_input_fingerprint,
    translate_input_fingerprint,
    translation_model_profile,
    translation_structure_fingerprint_part,
)
from trans_novel.pipeline.planning.planner import PrescanInputs, WorkflowPolicy
from trans_novel.pipeline.state import IdentityMismatchError, RunState, normalize_lang_code
from trans_novel.pipeline.state.models import TRANSLATION_POLICY_VERSION


def sample_text(doc, *, labeled: bool = True) -> str:
    """取风格分析样章。labeled=True 多点采样带中文标注；False 返回单段纯源文（语言检测用）。"""
    chapters = [
        chapter
        for chapter in doc.chapters
        if chapter.processing is None or chapter.processing.action != "preserve"
    ]
    texts = ["\n".join(s.source for s in ch.text_segments) for ch in chapters]
    texts = [t for t in texts if len(t) > 200]
    if not texts:
        return "\n".join(s.source for ch in chapters[:2] for s in ch.text_segments)[:6000]
    if not labeled:
        return texts[0][:6000]
    picks = [(0, "开头样章"), (len(texts) // 2, "中部样章"), (len(texts) - 1, "结尾样章")]
    parts: list[str] = []
    seen: set[int] = set()
    for idx, tag in picks:
        if idx in seen:  # 短书（1-2 章）去重
            continue
        seen.add(idx)
        t = texts[idx]
        chunk = t[-2800:] if tag == "结尾样章" else t[:2800]
        parts.append(f"【{tag}】\n{chunk}")
    return "\n\n".join(parts)


def _build_text_inputs(store, state):
    def source(ci):
        return "\n".join(s.source for s in store.load_chapter(ci).text_segments)

    def done_targets(*, include_preserved: bool = False):
        return "\n".join(
            "\n".join(s.target or "" for s in store.load_chapter(c.index).text_segments)
            for c in state.chapters
            if store.load_progress(c.index).status == "done"
            and (include_preserved or c.processing is None or c.processing.action != "preserve")
        )

    def titles():
        toc = state.meta.get("toc_entries") if isinstance(state.meta, dict) else []
        stored_chapters = [store.load_chapter(chapter.index) for chapter in state.chapters]
        preserved_entries = preserved_toc_entry_ids(stored_chapters, toc)
        values = [
            c.title
            for c in state.chapters
            if c.title and (c.processing is None or c.processing.action != "preserve")
        ]
        return values + [
            str(x.get("title", ""))
            for x in toc
            if isinstance(x, dict) and x.get("title") and x.get("entry_id") not in preserved_entries
        ]

    return source, done_targets, titles


def _check_policy(store, state: RunState, goal) -> bool:
    legacy = (
        store.exists() and state.identity.translation_policy_version != TRANSLATION_POLICY_VERSION
    )
    if legacy and {"prepare", "prescan", "translate", "titles", "repair", "polish"}.intersection(
        goal.phases
    ):
        raise IdentityMismatchError(
            "翻译策略版本不一致；请创建新的状态目录重新翻译，原有结果保持不变。"
        )
    return legacy


def _historical_inputs(inputs: PrescanInputs, state: RunState) -> PrescanInputs:
    def saved(key: str) -> str:
        node = state.nodes.get(key)
        return node.input_fingerprint if node else ""

    inputs.prepare_fingerprint = lambda: saved("prepare")
    inputs.analyze_fingerprint = lambda: saved("analyze")
    inputs.mine_fingerprint = lambda: saved("mine_terms")
    inputs.name_terms_fingerprint = lambda: saved("name_terms")
    inputs.translate_fingerprint = lambda ci: saved(f"translate:{ci}")
    inputs.polish_fingerprint = lambda ci: saved(f"polish:{ci}")
    inputs.titles_fingerprint = lambda: saved("titles")
    inputs.deterministic_qa_fingerprint = lambda: saved("deterministic_qa")
    inputs.report_fingerprint = lambda: saved("report")
    return inputs


def _preserve_paid_inputs(inputs: PrescanInputs, state: RunState) -> PrescanInputs:
    """输出目标不得因当前模型配置变化失效已付费的翻译链。"""

    def saved(key: str) -> str:
        node = state.nodes.get(key)
        return node.input_fingerprint if node else ""

    inputs.prepare_fingerprint = lambda: saved("prepare")
    inputs.analyze_fingerprint = lambda: saved("analyze")
    inputs.mine_fingerprint = lambda: saved("mine_terms")
    inputs.name_terms_fingerprint = lambda: saved("name_terms")
    inputs.translate_fingerprint = lambda ci: saved(f"translate:{ci}")
    inputs.polish_fingerprint = lambda ci: saved(f"polish:{ci}")
    inputs.titles_fingerprint = lambda: saved("titles")
    return inputs


def _output_fingerprint_inputs(context, output, goal, done_targets) -> dict:
    inventory = context.layout_inventory if context is not None else None

    def layout_fingerprint():
        profile = context.layout_profile
        return profile.digest if profile is not None else inventory.digest

    return {
        "layout_fingerprint": layout_fingerprint if inventory is not None else None,
        "layout_enabled": bool(
            context is not None
            and context.output_format == "epub"
            and context.theme_bundle is not None
            and context.theme_bundle.general_css is not None
        ),
        "layout_profile_valid": bool(context is not None and context.layout_profile is not None),
        "assemble_fingerprint": lambda: assemble_input_fingerprint(
            done_targets(include_preserved=True),
            mono=output.mono,
            bilingual=output.bilingual.enabled,
            out_format=goal.out_format,
            bilingual_order=output.bilingual.order,
            output_digest=context.output_digest if context is not None else None,
        ),
    }


def build_prescan_inputs(
    config: Config, store, policy: WorkflowPolicy, context, goal
) -> PrescanInputs:
    cfg = config
    output = context.output if context is not None else cfg.output
    state = store.load_state() if store.exists() else RunState()
    legacy = _check_policy(store, state, goal)
    src = state.identity.source_lang or normalize_lang_code(cfg.source_lang)
    tgt = state.identity.target_lang or normalize_lang_code(cfg.target_lang)
    source, done_targets, titles = _build_text_inputs(store, state)
    prepare_fp = lambda: prepare_input_fingerprint(state.identity.source_bytes_sha256, src, tgt)  # noqa: E731

    def analyze_fp():
        chapters = [
            Chapter(
                index=c.index,
                title=c.title,
                segments=store.load_chapter(c.index).text_segments,
                processing=c.processing,
            )
            for c in state.chapters
        ]
        doc = Document(
            title=state.title,
            fmt=state.fmt,
            source_lang=state.source_lang,
            target_lang=state.target_lang,
            source_path=state.source_path,
            chapters=chapters,
        )
        return analyze_input_fingerprint(sample_text(doc), analyst_model_profile(cfg))

    mine_fp = lambda: mine_terms_input_fingerprint(  # noqa: E731
        [
            source(c.index)
            for c in state.chapters
            if c.processing is None or c.processing.action != "preserve"
        ],
        src,
        policy.prescan_concurrency,
        fast_model_profile(cfg),
    )

    def translate_fp(ci):
        source_text = (
            source(ci)
            + "\n"
            + translation_structure_fingerprint_part(store.load_chapter(ci).text_segments)
        )
        chapter = next(c for c in state.chapters if c.index == ci)
        if chapter.processing is not None and chapter.processing.action == "preserve":
            return preserve_source_input_fingerprint(source_text, chapter.processing)
        return translate_input_fingerprint(
            source_text,
            src,
            tgt,
            style_brief=context.style_brief(),
            punctuation_normalize=cfg.punctuation_normalize,
            honorific_strategy=cfg.honorific_strategy,
            glossary_scope=cfg.pipeline.glossary_scope,
            single_segment_translation=cfg.pipeline.single_segment_translation,
            model=translation_model_profile(cfg),
            processing=chapter.processing,
        )

    polish_fp = lambda ci: polish_input_fingerprint(  # noqa: E731
        source(ci),
        src,
        context.style_brief(),
        punctuation_normalize=cfg.punctuation_normalize,
        model=polish_model_profile(cfg),
    )
    titles_fp = lambda: titles_input_fingerprint(titles(), src, tgt, analyst_model_profile(cfg))  # noqa: E731
    qa_fp = lambda: deterministic_qa_input_fingerprint(  # noqa: E731
        done_targets(),
        glossary_semantic_fingerprint_part(
            [term for term in context.glossary().all_terms() if getattr(term, "locked", 0)]
        ),
    )

    def report_fp():
        st = store.load_state()
        lint = [x for p in st.progress.values() for x in p.lint_issues]
        node = st.nodes.get("deterministic_qa")
        findings = (node.output or {}).get("issues", []) if node else []
        report_titles = [c.title for c in st.chapters if c.title]
        return report_input_fingerprint(
            lint, findings, [t.source for t in context.glossary().all_terms()], report_titles
        )

    inputs = PrescanInputs(
        prepare_fingerprint=prepare_fp,
        analyze_fingerprint=analyze_fp,
        mine_fingerprint=mine_fp,
        name_terms_fingerprint=lambda: name_terms_input_fingerprint(
            mine_fp(), context.style_brief(), policy.prescan_concurrency, analyst_model_profile(cfg)
        ),
        translate_fingerprint=translate_fp,
        polish_fingerprint=polish_fp,
        titles_fingerprint=titles_fp,
        deterministic_qa_fingerprint=qa_fp,
        report_fingerprint=report_fp,
        **_output_fingerprint_inputs(context, output, goal, done_targets),
    )
    if legacy:
        return _historical_inputs(inputs, state)
    if set(goal.phases).issubset({"layout", "assemble"}):
        return _preserve_paid_inputs(inputs, state)
    return inputs


__all__ = ["build_prescan_inputs"]
