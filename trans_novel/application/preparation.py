"""文档准备阶段的框架无关协调逻辑。

本模块只规定运行目录发现、源文件身份校验和初始化事务的执行顺序。具体文档
解析、旧状态存储、LLM Agent、术语库和锁均由窄端口提供，因此这里不依赖
``RunStore``、``Config``、LangGraph 或任何模型客户端。
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ContextManager, Generic, Protocol, TypeVar

ProgressFn = Callable[[int, int, str], None]


class PreparationDocument(Protocol):
    """准备协调器需要的最小可变文档视图。"""

    title: str
    fmt: str
    source_lang: str
    target_lang: str
    chapters: Sequence[object]


StateT = TypeVar("StateT")
DocumentT = TypeVar("DocumentT", bound=PreparationDocument)
GlossaryT = TypeVar("GlossaryT")
LockedInitializer = Callable[..., StateT]


@dataclass(frozen=True, slots=True)
class InitializationEventConfig:
    """写入旧初始化事件的稳定配置快照。"""

    review: bool
    polish: bool
    backtranslate_sample: float
    book_understanding: bool
    review_concurrency: int
    review_output_retries: int

    def as_mapping(self) -> dict[str, object]:
        """生成与旧 ``run_initialized`` 事件完全一致的新字典。"""
        return {
            "review": self.review,
            "polish": self.polish,
            "backtranslate_sample": self.backtranslate_sample,
            "book_understanding": self.book_understanding,
            "review_concurrency": self.review_concurrency,
            "review_output_retries": self.review_output_retries,
        }


@dataclass(frozen=True, slots=True)
class PreparationPolicy:
    """一次准备调用需要的不可变路径、语言和切分策略。"""

    state_dir: str
    source_lang: str | None
    target_lang: str
    max_chars_per_segment: int
    rolling_context_segments: int
    initialization_event: InitializationEventConfig


class PreparationPort(Protocol[StateT, DocumentT, GlossaryT]):
    """把准备事务接到具体解析器、状态存储和分析器的最小端口。"""

    def state_for_title(self, state_dir: str, title: str, *, create: bool) -> StateT:
        """定位书名对应的状态对象，并按需创建目录。"""
        ...

    def state_lock(self, state: StateT) -> ContextManager[None]:
        """返回覆盖整段初始化或恢复检查的书级排他锁。"""
        ...

    def state_exists(self, state: StateT) -> bool:
        """仅在初始化完成标志存在时返回真。"""
        ...

    def state_run_dir(self, state: StateT) -> str:
        """返回事件使用的运行目录。"""
        ...

    def source_cache_dir(self, state: StateT) -> str:
        """返回昂贵输入转换的内容寻址缓存根目录。"""
        ...

    def bind_state(self, state: StateT, progress: ProgressFn | None) -> None:
        """把事件、活动进度和调用指标绑定到当前状态。"""
        ...

    def load_document(
        self,
        input_path: str,
        *,
        source_lang: str | None,
        target_lang: str,
        max_chars_per_segment: int,
        cache_dir: str | None = None,
        source_hash: str | None = None,
    ) -> DocumentT:
        """解析输入；只有 PDF 首次准备才提供缓存目录和源摘要。"""
        ...

    def initial_source_hash(self, input_path: str) -> str:
        """在解析前捕获源文件身份。"""
        ...

    def verified_source_hash(self, input_path: str) -> str:
        """重新读取或验证调用内源文件身份。"""
        ...

    def ensure_state_source(self, state: StateT, input_path: str) -> str:
        """拒绝把既有状态用于不同内容的源文件。"""
        ...

    def begin_initialization(self, state: StateT, source_hash: str) -> None:
        """写初始化标志并清理上次未提交的派生状态。"""
        ...

    def finish_initialization(self, state: StateT) -> None:
        """在正式清单提交后移除初始化标志。"""
        ...

    def detect_language(self, document: DocumentT) -> str:
        """返回旧兼容规则下的语言候选；无法识别时返回空串。"""
        ...

    def apply_language(self, source_lang: str) -> None:
        """把已解析语言同步到当前调用的配置和 Agent。"""
        ...

    def stage_document(
        self,
        state: StateT,
        document: DocumentT,
        *,
        source_hash: str,
    ) -> dict[str, Any]:
        """写章节等可恢复中间产物，但不提交正式清单。"""
        ...

    def open_glossary(self, state: StateT) -> GlossaryT:
        """打开本次风格分析使用的术语库。"""
        ...

    def close_glossary(self, glossary: GlossaryT) -> None:
        """关闭术语库并释放其文件句柄。"""
        ...

    def analyze(self, sample: str) -> dict[str, Any]:
        """分析全书风格样本。"""
        ...

    def sample_text(self, document: DocumentT) -> str:
        """提取与旧风格分析一致的确定性文档样本。"""
        ...

    def seed_glossary(self, glossary: GlossaryT, analysis: Mapping[str, Any]) -> None:
        """把分析产出的初始术语写入术语库。"""
        ...

    def save_analysis(self, state: StateT, analysis: dict[str, Any]) -> None:
        """保存风格分析快照。"""
        ...

    def save_initial_context(self, state: StateT, *, max_recent_keep: int) -> None:
        """创建旧翻译器需要的空滚动上下文。"""
        ...

    def save_manifest(self, state: StateT, manifest: dict[str, Any]) -> None:
        """原子提交初始化完成清单。"""
        ...

    def emit_event(self, state: StateT, event: str, **attributes: object) -> None:
        """写入旧版可恢复事件流。"""
        ...


@dataclass(frozen=True, slots=True)
class PreparationCoordinator(Generic[StateT, DocumentT, GlossaryT]):
    """按旧运行时语义协调一次定位、恢复或全新初始化。"""

    policy: PreparationPolicy
    port: PreparationPort[StateT, DocumentT, GlossaryT]
    locked_initializer: LockedInitializer[StateT] | None = None

    def locate_existing(
        self,
        input_path: str,
        *,
        progress: ProgressFn | None = None,
    ) -> StateT:
        """只定位既有状态，不创建目录或触发昂贵的 PDF 转换。"""
        title = self._locatable_title(input_path, progress=progress)
        state = self.port.state_for_title(self.policy.state_dir, title, create=False)
        if not self.port.state_exists(state):
            raise ValueError("尚无翻译进度。请先运行 translate。")

        # 身份校验必须早于事件/指标绑定，避免错误任务污染既有运行账本。
        self.port.ensure_state_source(state, input_path)
        self.port.bind_state(state, progress)
        return state

    def prepare(
        self,
        input_path: str,
        *,
        progress: ProgressFn | None = None,
    ) -> StateT:
        """解析输入并恢复或初始化状态，保持旧版 PDF 快速续跑路径。"""
        if self._is_pdf(input_path):
            return self._prepare_pdf(input_path, progress=progress)

        # 普通格式的目录取决于文档标题，所以解析和稳定性校验先于加书级锁。
        self._publish_progress(progress, "解析文档…")
        source_hash = self.port.initial_source_hash(input_path)
        document = self.port.load_document(
            input_path,
            source_lang=self.policy.source_lang,
            target_lang=self.policy.target_lang,
            max_chars_per_segment=self.policy.max_chars_per_segment,
        )
        if self.port.verified_source_hash(input_path) != source_hash:
            raise ValueError("源文件在解析期间发生变化；请确认文件稳定后重试。")

        state = self.port.state_for_title(
            self.policy.state_dir,
            document.title,
            create=True,
        )
        self.port.bind_state(state, progress)
        with self.port.state_lock(state):
            return self._invoke_locked_initializer(
                document,
                state,
                input_path,
                progress,
                source_hash=source_hash,
            )

    def initialize_locked(
        self,
        document: DocumentT,
        state: StateT,
        input_path: str,
        progress: ProgressFn | None,
        *,
        source_hash: str | None = None,
    ) -> StateT:
        """在调用方持有书级锁时恢复状态，或提交一笔可崩溃恢复的初始化。"""
        if self.port.state_exists(state):
            self.port.ensure_state_source(state, input_path)
            self._emit_resume(state, input_path)
            return state

        # 初始化标志先于任何派生写入；失败重试会据此清理半成品，而 PDF
        # 内容寻址缓存仍可保留。正式 manifest 是唯一完成标志，必须最后提交。
        initialization_hash = source_hash or self.port.verified_source_hash(input_path)
        self.port.begin_initialization(state, initialization_hash)
        self._resolve_language(document, state, progress=progress)

        manifest = self.port.stage_document(
            state,
            document,
            source_hash=initialization_hash,
        )
        glossary = self.port.open_glossary(state)
        try:
            self._publish_progress(progress, "分析全书风格…")
            sample = self.port.sample_text(document)
            analysis = self.port.analyze(sample) if sample else {}
            if analysis:
                self.port.seed_glossary(glossary, analysis)
            self.port.save_analysis(state, analysis)
            self.port.emit_event(state, "analysis_saved", has_analysis=bool(analysis))
            self.port.save_initial_context(
                state,
                max_recent_keep=max(40, self.policy.rolling_context_segments),
            )

            # 清单先原子落盘，再清掉 initializing 标志；任一步骤崩溃后，
            # ``state_exists`` 都只会把完整清单视为可续跑任务。
            manifest["initialized"] = True
            self.port.save_manifest(state, manifest)
            self.port.finish_initialization(state)
            self._emit_initialized(state, input_path, document)
        finally:
            self.port.close_glossary(glossary)
        return state

    def _prepare_pdf(
        self,
        input_path: str,
        *,
        progress: ProgressFn | None,
    ) -> StateT:
        """在转换前定位并锁定 PDF 状态，续跑时完全跳过外部转换。"""
        title = os.path.splitext(os.path.basename(input_path))[0]
        state = self.port.state_for_title(self.policy.state_dir, title, create=True)
        self.port.bind_state(state, progress)
        with self.port.state_lock(state):
            if self.port.state_exists(state):
                self.port.ensure_state_source(state, input_path)
                self._emit_resume(state, input_path)
                return state

            self._publish_progress(progress, "解析文档…")
            source_hash = self.port.initial_source_hash(input_path)
            # 转换前留下初始化标志，使失败重试延续同源事件和指标账本。
            self.port.begin_initialization(state, source_hash)
            document = self.port.load_document(
                input_path,
                source_lang=self.policy.source_lang,
                target_lang=self.policy.target_lang,
                max_chars_per_segment=self.policy.max_chars_per_segment,
                cache_dir=self.port.source_cache_dir(state),
                source_hash=source_hash,
            )
            if self.port.verified_source_hash(input_path) != source_hash:
                raise ValueError("PDF 在解析期间发生变化；请确认文件稳定后重试。")
            return self._invoke_locked_initializer(
                document,
                state,
                input_path,
                progress,
                source_hash=source_hash,
            )

    def _invoke_locked_initializer(
        self,
        document: DocumentT,
        state: StateT,
        input_path: str,
        progress: ProgressFn | None,
        *,
        source_hash: str,
    ) -> StateT:
        """经兼容入口进入初始化，使旧子类覆写仍能拦截 ``prepare``。

        新运行时不提供回调时直接执行应用协调逻辑；旧适配器则注入原有
        ``_prepare_locked`` 方法。该方法的默认实现最终显式调用
        ``initialize_locked``，因此不会形成递归。
        """
        initializer = self.locked_initializer or self.initialize_locked
        return initializer(
            document,
            state,
            input_path,
            progress,
            source_hash=source_hash,
        )

    def _locatable_title(
        self,
        input_path: str,
        *,
        progress: ProgressFn | None,
    ) -> str:
        """用无副作用的最短路径确定既有任务书名。"""
        if self._is_pdf(input_path):
            return os.path.splitext(os.path.basename(input_path))[0]
        self._publish_progress(progress, "查找翻译进度…")
        document = self.port.load_document(
            input_path,
            source_lang=self.policy.source_lang,
            target_lang=self.policy.target_lang,
            max_chars_per_segment=self.policy.max_chars_per_segment,
        )
        return document.title

    def _resolve_language(
        self,
        document: DocumentT,
        state: StateT,
        *,
        progress: ProgressFn | None,
    ) -> None:
        """保持旧 auto 检测、失败事件和用户错误文案。"""
        if self.policy.source_lang in ("auto", "", None):
            self._publish_progress(progress, "识别语言…")
            detected = self.port.detect_language(document)
            if not detected:
                self.port.emit_event(
                    state,
                    "language_detection_failed",
                    source_lang=document.source_lang,
                )
                raise RuntimeError(
                    "自动识别源语言失败：请检查模型配置，或在 config.yaml 的 "
                    "language.source 指定 ISO 639-1 语言代码（如 ja/en/ko/ru/fr/de/es）。"
                )
            document.source_lang = detected
            self.port.emit_event(
                state,
                "language_detected",
                source_lang=document.source_lang,
            )
        self.port.apply_language(document.source_lang)

    def _emit_resume(self, state: StateT, input_path: str) -> None:
        """发布与旧编排器相同的续跑事件。"""
        self.port.emit_event(
            state,
            "run_resumed",
            input_path=input_path,
            run_dir=self.port.state_run_dir(state),
        )

    def _emit_initialized(
        self,
        state: StateT,
        input_path: str,
        document: DocumentT,
    ) -> None:
        """在清单提交成功后发布完整初始化事件。"""
        self.port.emit_event(
            state,
            "run_initialized",
            input_path=input_path,
            run_dir=self.port.state_run_dir(state),
            title=document.title,
            fmt=document.fmt,
            source_lang=document.source_lang,
            target_lang=document.target_lang,
            chapters=len(document.chapters),
            config=self.policy.initialization_event.as_mapping(),
        )

    @staticmethod
    def _is_pdf(input_path: str) -> bool:
        """按旧规则仅通过不区分大小写的扩展名识别 PDF。"""
        return os.path.splitext(input_path)[1].lower() == ".pdf"

    @staticmethod
    def _publish_progress(progress: ProgressFn | None, message: str) -> None:
        """保持旧未知总量进度回调的三元参数形状。"""
        if progress:
            progress(0, 0, message)


__all__ = [
    "InitializationEventConfig",
    "LockedInitializer",
    "PreparationCoordinator",
    "PreparationDocument",
    "PreparationPolicy",
    "PreparationPort",
    "ProgressFn",
]
