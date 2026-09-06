"""prepare 与 analyze 节点：解析/语言解析/身份/暂存 + 风格分析与术语播种。

- prepare：解析源文件、解析源语言（auto 检测；请求异常原样向上抛，绝不兜底）、
  建立/核验 V2 RunIdentity、暂存文档与上下文。新运行把暂存 manifest 作为产物
  交给 analyze 节点，由 analyze 成功后才原子落盘（manifest-last：分析失败不写
  manifest，续跑整段重来；node 状态而非 manifest 决定初始化完整性）。
- analyze：风格/角色分析与术语播种；幂等（analysis.json 已存在即跳过）。
"""

from __future__ import annotations

from trans_novel.agents.base import WorkflowProtocolError, retry_protocol
from trans_novel.config import Config
from trans_novel.glossary.store import GlossaryStore
from trans_novel.ingest import Document
from trans_novel.llm.base import LLMClient
from trans_novel.pipeline.contracts import NodeOutcome, NodeRequest
from trans_novel.pipeline.planning import (
    analyst_model_profile,
    analyze_input_fingerprint,
    frozen_input_fingerprint,
    prepare_input_fingerprint,
)
from trans_novel.pipeline.planning.prescan import sample_text
from trans_novel.pipeline.state import (
    NODE_ANALYZE,
    NODE_PREPARE,
    RUN_INPUT_SCHEMA_VERSION,
    SCOPE_BOOK,
    RollingContext,
    RunIdentity,
    normalize_lang_code,
    source_bytes_hash,
)


def _normalize_lang(code: str) -> str:
    return normalize_lang_code(code)


class PrepareNode:
    """根准备节点：解析、语言、身份、暂存。"""

    node_id = NODE_PREPARE
    scope = SCOPE_BOOK

    def __init__(self, *, client: LLMClient, config: Config, doc: Document | None):
        self.client = client
        self.config = config
        self.doc = doc

    def execute(self, request: NodeRequest) -> NodeOutcome:
        store = request.store
        doc = self.doc
        progress = request.progress
        if progress:
            progress(0, 0, "读取原书…")

        if store.exists():
            # 已有进度 → 先核验运行身份（源文件字节/解析 schema/语言），失配拒绝复用。
            store.verify_identity(
                source_bytes_sha256=source_bytes_hash(request.input_path),
                source_lang=normalize_lang_code(self.config.source_lang),
                target_lang=normalize_lang_code(self.config.target_lang),
            )
            store.log_event("run_resumed", input_path=request.input_path, run_dir=store.run_dir)
            state = store.load_state()
            request.shared.resolved_source_lang = state.source_lang or ""
            fp = prepare_input_fingerprint(
                state.identity.source_bytes_sha256,
                state.identity.source_lang,
                state.identity.target_lang,
            )
            return NodeOutcome(fingerprint=fp)

        # 新建：auto 时只使用模型检测主要语言；失败则要求用户显式指定。
        assert doc is not None, "全新运行必须携带解析后的文档"
        if self.config.source_lang in ("auto", "", None):
            if progress:
                progress(0, 0, "识别语言…")

            def detect() -> str:
                detected = self._detect_language_ai(doc)
                if detected:
                    return detected
                store.log_event("language_detection_failed", source_lang=doc.source_lang)
                raise WorkflowProtocolError(
                    "language_detection_invalid",
                    "自动识别源语言失败：请检查模型配置，或用 --source-language "
                    "指定 ISO 639-1 语言代码（如 ja/en/ko/ru/fr/de/es）。",
                )

            detected = retry_protocol(detect, retries=self.config.pipeline.protocol_retry_limit)
            doc.source_lang = detected
            store.log_event("language_detected", source_lang=doc.source_lang)

        source = _normalize_lang(doc.source_lang)
        target = _normalize_lang(self.config.target_lang)
        if source and target and source == target:
            raise ValueError(
                f"源语言与目标语言相同（{source}），无需翻译；"
                "请用 --source-language 指定正确的源语言。"
            )
        request.shared.resolved_source_lang = doc.source_lang

        identity = RunIdentity(
            source_bytes_sha256=source_bytes_hash(request.input_path),
            run_input_schema_version=RUN_INPUT_SCHEMA_VERSION,
            source_lang=source,
            target_lang=target,
        )
        manifest = store.stage_document(doc, identity)
        fp = prepare_input_fingerprint(identity.source_bytes_sha256, source, target)
        return NodeOutcome(fingerprint=fp, artifacts={"manifest": manifest})

    def _detect_language_ai(self, doc: Document) -> str:
        """用模型检测正文主要语言；模型无法判断时返回空串，请求异常直接向上抛出。"""
        sample = sample_text(doc, labeled=False)[:1500]
        if not sample.strip():
            return ""
        system = (
            "你是语言识别器。判断给定文本的主要自然语言，"
            '仅输出 JSON：{"language":"<ISO 639-1 两字母代码，如 ja/en/ru/ko/fr/de/zh>"}。'
            "无法判断时 language 置为空字符串。"
        )
        data = self.client.complete_json(
            [{"role": "system", "content": system}, {"role": "user", "content": sample}],
            stage="language_detect",
            agent="preparer",
            operation="language.detect",
        )
        code = (data.get("language") if isinstance(data, dict) else "") or ""
        return _normalize_lang(str(code))


class AnalyzeNode:
    """风格/角色分析与术语播种；初始化完成（manifest 落盘）的唯一完成点。"""

    node_id = NODE_ANALYZE
    scope = SCOPE_BOOK

    def __init__(
        self,
        *,
        analyzer,
        config: Config,
        doc: Document | None,
        glossary: GlossaryStore,
        frozen_book=None,
    ):
        self.analyzer = analyzer
        self.config = config
        self.doc = doc
        self.glossary = glossary
        self.frozen_book = frozen_book

    def execute(self, request: NodeRequest) -> NodeOutcome:
        store = request.store
        if self.frozen_book is not None:
            analysis = dict(self.frozen_book.analysis)
            store.save_analysis(analysis)
            if not store.exists():
                manifest = (request.artifacts.get("prepare") or {}).get("manifest")
                if manifest is not None:
                    manifest["initialized"] = True
                    store.save_manifest(manifest)
            if self.doc is not None:
                request.shared.resolved_source_lang = self.doc.source_lang
            fp = frozen_input_fingerprint(
                request.shared.frozen_preparation.preparation_sha256,
                self.node_id,
                self.frozen_book.book_id,
                self.frozen_book.analysis,
            )
            return NodeOutcome(fingerprint=fp)
        if store.load_analysis() is not None:
            # 幂等：已分析过（续跑）。但 analysis.json 存在而 manifest 不存在 =
            # 崩溃窗口（save_analysis 之后、save_manifest 之前）——必须用本轮
            # staged manifest 原子补完初始化，否则初始化永远无法恢复。
            if not store.exists():
                manifest = (request.artifacts.get("prepare") or {}).get("manifest")
                if manifest is not None:
                    manifest["initialized"] = True
                    store.save_manifest(manifest)
            fp = analyze_input_fingerprint(
                sample_text(self.doc) if self.doc else "", analyst_model_profile(self.config)
            )
            return NodeOutcome(fingerprint=fp)
        if request.progress:
            request.progress(0, 0, "分析全书风格…")
        doc = self.doc
        assert doc is not None, "初始化阶段的 analyze 必须携带解析后的文档"
        sample = sample_text(doc)
        analysis = self.analyzer.analyze(sample) if sample else {}
        if analysis:
            self.analyzer.seed_glossary(self.glossary, analysis)
        store.save_analysis(analysis)
        store.log_event("analysis_saved", has_analysis=bool(analysis))
        store.save_context(
            RollingContext(
                max_recent_keep=max(40, self.config.pipeline.rolling_context_segments)
            ).to_dict()
        )
        # manifest 是初始化完成标志，必须最后原子落盘（分析失败时不写）。
        manifest = (request.artifacts.get("prepare") or {}).get("manifest")
        if manifest is None:
            manifest = store.load_manifest()
        manifest["initialized"] = True
        store.save_manifest(manifest)
        store.log_event(
            "run_initialized",
            input_path=request.input_path,
            run_dir=store.run_dir,
            title=doc.title,
            fmt=doc.fmt,
            source_lang=doc.source_lang,
            target_lang=doc.target_lang,
            chapters=len(doc.chapters),
            config={
                "polish": self.config.pipeline.polish,
                "back_matter": self.config.pipeline.back_matter,
            },
        )
        fp = analyze_input_fingerprint(sample, analyst_model_profile(self.config))
        return NodeOutcome(fingerprint=fp)


__all__ = ["AnalyzeNode", "PrepareNode"]
