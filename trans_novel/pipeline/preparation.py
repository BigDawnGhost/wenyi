"""旧版流水线到框架无关文档准备协调器的适配。"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from typing import Any, Protocol

from ..application.preparation import (
    InitializationEventConfig,
    PreparationCoordinator,
    PreparationPolicy,
)
from ..config import Config
from ..glossary.store import GlossaryStore
from ..ingest.models import Document
from .context import RollingContext
from .runstore import RunStore, slugify

ProgressFn = Callable[[int, int, str], None]
DocumentLoader = Callable[..., Document]


class LegacyPreparationHost(Protocol):
    """旧编排器暴露给准备适配器的最小兼容接口。"""

    config: Config
    analyzer: Any

    def _bind_llm_events(
        self,
        store: RunStore,
        progress: ProgressFn | None = None,
    ) -> None:
        """绑定旧 LLM 事件和活动回调。"""
        ...

    def _attach_metrics_store(self, store: RunStore) -> None:
        """把当前运行指标绑定到状态目录。"""
        ...

    def _initial_source_sha256(self, input_path: str) -> str:
        """返回解析前源文件摘要。"""
        ...

    def _source_sha256(self, input_path: str) -> str:
        """返回当前已验证源文件摘要。"""
        ...

    def _ensure_store_source(self, store: RunStore, input_path: str) -> str:
        """校验状态与输入内容身份一致。"""
        ...

    def _detect_language_ai(self, document: Document) -> str:
        """用旧模型适配器检测源语言。"""
        ...

    def _sample_text(self, document: Document, *, labeled: bool = True) -> str:
        """提取旧风格分析或语言检测样本。"""
        ...

    def _apply_language(self, source_lang: str) -> None:
        """把源语言同步到旧配置和全部 Agent。"""
        ...

    def _prepare_locked(
        self,
        document: Document,
        store: RunStore,
        input_path: str,
        progress: ProgressFn | None,
        *,
        source_hash: str | None = None,
    ) -> RunStore:
        """执行旧可覆写的持锁初始化入口。"""
        ...


class LegacyPreparationPort:
    """把 ``RunStore``、旧 Agent 与解析器收窄为准备阶段端口。"""

    def __init__(self, host: LegacyPreparationHost, loader: DocumentLoader) -> None:
        self._host = host
        self._loader = loader

    def state_for_title(self, state_dir: str, title: str, *, create: bool) -> RunStore:
        """按旧 slug 规则构造一本书的状态对象。"""
        return RunStore(os.path.join(state_dir, slugify(title)), create=create)

    @staticmethod
    def state_lock(state: RunStore) -> AbstractContextManager[None]:
        """返回旧版覆盖整本书操作的排他锁。"""
        return state.lock()

    @staticmethod
    def state_exists(state: RunStore) -> bool:
        """以正式 manifest 是否存在判断初始化完成。"""
        return state.exists()

    @staticmethod
    def state_run_dir(state: RunStore) -> str:
        """返回旧运行目录。"""
        return state.run_dir

    @staticmethod
    def source_cache_dir(state: RunStore) -> str:
        """返回 PDF 内容寻址缓存根目录。"""
        return state.source_dir

    def bind_state(self, state: RunStore, progress: ProgressFn | None) -> None:
        """保持旧顺序绑定 LLM 事件，再绑定调用指标。"""
        self._host._bind_llm_events(state, progress)
        self._host._attach_metrics_store(state)

    def load_document(
        self,
        input_path: str,
        *,
        source_lang: str | None,
        target_lang: str,
        max_chars_per_segment: int,
        cache_dir: str | None = None,
        source_hash: str | None = None,
    ) -> Document:
        """保持旧文档解析参数，并仅在 PDF 路径传入缓存参数。"""
        kwargs: dict[str, object] = {
            "split_segments": max_chars_per_segment,
        }
        if cache_dir is not None:
            kwargs["cache_dir"] = cache_dir
        if source_hash is not None:
            kwargs["source_hash"] = source_hash
        return self._loader(input_path, source_lang, target_lang, **kwargs)

    def initial_source_hash(self, input_path: str) -> str:
        """委托旧调用级源身份快照。"""
        return self._host._initial_source_sha256(input_path)

    def verified_source_hash(self, input_path: str) -> str:
        """委托旧调用级源身份复验。"""
        return self._host._source_sha256(input_path)

    def ensure_state_source(self, state: RunStore, input_path: str) -> str:
        """在消费既有状态前执行旧源身份校验。"""
        return self._host._ensure_store_source(state, input_path)

    @staticmethod
    def begin_initialization(state: RunStore, source_hash: str) -> None:
        """先写标志并清理半成品，保留同源 PDF 缓存和失败账本。"""
        state.begin_initialization(source_hash)

    @staticmethod
    def finish_initialization(state: RunStore) -> None:
        """仅在 manifest 已提交后清除临时标志。"""
        state.finish_initialization()

    def detect_language(self, document: Document) -> str:
        """复用旧检测错误收敛规则。"""
        return self._host._detect_language_ai(document)

    def apply_language(self, source_lang: str) -> None:
        """复用旧配置和 Agent 语言同步逻辑。"""
        self._host._apply_language(source_lang)

    @staticmethod
    def stage_document(
        state: RunStore,
        document: Document,
        *,
        source_hash: str,
    ) -> dict[str, Any]:
        """写章节中间状态，但不让 manifest 提前成为完成标志。"""
        return state.stage_document(document, source_hash=source_hash)

    @staticmethod
    def open_glossary(state: RunStore) -> GlossaryStore:
        """打开旧 SQLite 术语库。"""
        return GlossaryStore(state.glossary_path)

    @staticmethod
    def close_glossary(glossary: GlossaryStore) -> None:
        """无论分析是否成功都关闭术语库。"""
        glossary.close()

    def analyze(self, sample: str) -> dict[str, Any]:
        """调用旧风格分析 Agent。"""
        return self._host.analyzer.analyze(sample)

    def sample_text(self, document: Document) -> str:
        """经旧私有 seam 提取风格样本，保留子类与 monkeypatch 兼容。"""
        return self._host._sample_text(document)

    def seed_glossary(
        self,
        glossary: GlossaryStore,
        analysis: Mapping[str, Any],
    ) -> None:
        """把分析术语交给旧 Agent 写入旧术语库。"""
        self._host.analyzer.seed_glossary(glossary, analysis)

    @staticmethod
    def save_analysis(state: RunStore, analysis: dict[str, Any]) -> None:
        """保存与旧 manifest 分离的分析快照。"""
        state.save_analysis(analysis)

    @staticmethod
    def save_initial_context(state: RunStore, *, max_recent_keep: int) -> None:
        """保存空滚动上下文，供后续旧翻译阶段续写。"""
        state.save_context(RollingContext(max_recent_keep=max_recent_keep).to_dict())

    @staticmethod
    def save_manifest(state: RunStore, manifest: dict[str, Any]) -> None:
        """原子提交旧初始化完成清单。"""
        state.save_manifest(manifest)

    @staticmethod
    def emit_event(state: RunStore, event: str, **attributes: object) -> None:
        """保持旧事件名称、属性和追加式日志格式。"""
        state.log_event(event, **attributes)


def build_legacy_preparation(
    host: LegacyPreparationHost,
    *,
    loader: DocumentLoader,
) -> PreparationCoordinator[RunStore, Document, GlossaryStore]:
    """从当前旧配置快照构造一次准备协调器。

    每次调用都重建策略，避免恢复 manifest 后被修改的语言配置泄漏到下一次
    调用；端口本身不保存任务状态或数据库连接。
    """
    config = host.config
    pipeline = config.pipeline
    policy = PreparationPolicy(
        state_dir=config.state_dir,
        source_lang=config.source_lang,
        target_lang=config.target_lang,
        max_chars_per_segment=config.segment.max_chars_per_segment,
        rolling_context_segments=pipeline.rolling_context_segments,
        initialization_event=InitializationEventConfig(
            review=pipeline.review,
            polish=pipeline.polish,
            backtranslate_sample=pipeline.backtranslate_sample,
            book_understanding=pipeline.book_understanding,
            review_concurrency=pipeline.review_concurrency,
            review_output_retries=pipeline.review_output_retries,
        ),
    )
    return PreparationCoordinator(
        policy=policy,
        port=LegacyPreparationPort(host, loader),
        locked_initializer=host._prepare_locked,
    )


__all__ = [
    "DocumentLoader",
    "LegacyPreparationHost",
    "LegacyPreparationPort",
    "build_legacy_preparation",
]
