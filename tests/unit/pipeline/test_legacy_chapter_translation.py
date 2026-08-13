"""旧版正文翻译拆分后的兼容 seam、持久化顺序和依赖边界合同。"""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

import pytest

from trans_novel.ingest.models import Chapter, Segment
from trans_novel.pipeline.chapter_translation import (
    BatchResult,
    TranslationPolicy,
    translate_legacy_chapter,
)
from trans_novel.pipeline.orchestrator import Orchestrator, _BatchResult


class _ContextSpy:
    """记录旧滚动上下文的读取和推进顺序。"""

    def __init__(self, actions: list[str]) -> None:
        self.actions = actions
        self.recent_targets: list[str] = []

    def render(self, count: int) -> str:
        """返回稳定上下文，并把读取动作加入统一时间线。"""
        self.actions.append(f"render:{count}")
        return "context"

    def add_targets(self, targets: list[str]) -> None:
        """模拟旧上下文追加及尾部保留。"""
        self.actions.append("context_add")
        self.recent_targets.extend(targets)


class _StoreSpy:
    """以单一时间线模拟正文翻译所需的最小旧 ``RunStore``。"""

    def __init__(
        self,
        chapter: Chapter,
        actions: list[str],
        *,
        checkpoints: set[str] | None = None,
        fail_save: bool = False,
    ) -> None:
        self.chapter = chapter
        self.actions = actions
        self.checkpoints = checkpoints or set()
        self.fail_save = fail_save
        self.events: list[tuple[str, dict[str, object]]] = []

    def load_chapter(self, chapter_index: int) -> Chapter:
        """返回测试中的同一可变章节对象。"""
        assert chapter_index == self.chapter.index
        return self.chapter

    @staticmethod
    def batch_glossary_key(start_index: int, count: int) -> str:
        """复现旧事件检查点键格式。"""
        return f"{start_index}:{count}"

    def completed_batch_glossary_keys(self, chapter_index: int) -> set[str]:
        """返回事件日志已确认的批次术语检查点。"""
        assert chapter_index == self.chapter.index
        return set(self.checkpoints)

    def save_chapter(self, chapter: Chapter) -> None:
        """记录正文的首个持久化边界，并按需模拟落盘失败。"""
        assert chapter is self.chapter
        self.actions.append("save_chapter")
        if self.fail_save:
            raise OSError("chapter write failed")

    def save_chapter_with_status(self, chapter: Chapter, status: str) -> None:
        """记录正文与 manifest 状态的原子发布边界。"""
        assert chapter is self.chapter
        self.actions.append(f"save_chapter_with_status:{status}")

    def set_chapter_status(self, chapter_index: int, status: str) -> None:
        """支持空章节的旧状态发布路径。"""
        assert chapter_index == self.chapter.index
        self.actions.append(f"set_chapter_status:{status}")

    def log_event(self, event: str, **attributes: object) -> None:
        """记录事件及其载荷，供顺序和恢复语义断言。"""
        self.actions.append(f"event:{event}")
        self.events.append((event, attributes))


def _policy() -> TranslationPolicy:
    """构造能让每个测试章节形成一个物理批次的旧策略。"""
    return TranslationPolicy(max_chars_per_batch=10_000)


def _coordinator_callbacks(actions: list[str]) -> dict[str, Any]:
    """构造按调用顺序打点的旧兼容回调集合。"""

    def process_batch(*_args: object, **_kwargs: object) -> BatchResult:
        actions.append("process_batch")
        return BatchResult(targets=["译文"], bt_samples=[("原文", "译文")])

    def term_snapshot(_glossary: object, _segments: list[Segment]) -> list[object]:
        actions.append("term_snapshot")
        return []

    def extract_batch_glossary(*_args: object, **_kwargs: object) -> dict[str, int]:
        actions.append("extract_batch_glossary")
        return {"inserted": 1}

    def align_after_batch(*_args: object) -> None:
        actions.append("align_after_batch")

    def sync_context(*_args: object) -> None:
        actions.append("sync_context")

    def update_history(*_args: object) -> None:
        actions.append("update_history")

    def annotation_contexts(
        _segments: list[Segment],
        _registry: dict[str, Any] | None,
    ) -> list[list[dict[str, str]]]:
        actions.append("annotation_contexts")
        return [[]]

    def extract_chapter_glossary(*_args: object, **_kwargs: object) -> dict[str, int]:
        actions.append("extract_chapter_glossary")
        return {"inserted": 0}

    def backtranslation_check(
        _sources: list[str],
        _targets: list[str],
    ) -> list[dict[str, str]]:
        actions.append("backtranslation_check")
        return [{"type": "semantic"}]

    return {
        "process_batch": process_batch,
        "term_snapshot": term_snapshot,
        "extract_batch_glossary": extract_batch_glossary,
        "align_after_batch": align_after_batch,
        "sync_context_chapter_prefix": sync_context,
        "update_translation_history": update_history,
        "annotation_contexts_for_segments": annotation_contexts,
        "chapter_progress_label": lambda _title, _index: "章节",
        "extract_chapter_glossary": extract_chapter_glossary,
        "backtranslation_check": backtranslation_check,
        "polish_enabled": lambda: True,
        "punctuation_enabled": lambda: False,
        "rolling_context_segments": lambda: 7,
    }


def test_batch_result_preserves_the_historical_type_alias_and_instance_shape() -> None:
    """旧导入名必须指向同一普通 dataclass，且继续暴露实例 ``__dict__``。"""
    assert _BatchResult is BatchResult
    result = _BatchResult(targets=["译文"])
    assert isinstance(result, BatchResult)
    assert result.__dict__ == {"targets": ["译文"], "bt_samples": []}


def test_orchestrator_facade_injects_every_dynamic_legacy_override_seam() -> None:
    """旧私有入口在调用时注入 bound method，保护 monkeypatch 和子类覆写。"""
    orchestrator = object.__new__(Orchestrator)
    pipeline = SimpleNamespace(
        polish=True,
        glossary_scope="chapter",
        rolling_context_segments=13,
        backtranslate_sample=0.25,
    )
    orchestrator.config = SimpleNamespace(
        segment=SimpleNamespace(max_chars_per_batch=99),
        pipeline=pipeline,
    )

    # 逐个安装实例级 seam，便于核对协调器收到的正是运行时 bound callback。
    seam_names = (
        "_process_batch",
        "_chapter_term_snapshot",
        "_extract_batch_glossary",
        "_align_annotations_after_batch",
        "_sync_context_chapter_prefix",
        "_update_translation_history",
        "_annotation_contexts_for_segments",
        "_chapter_progress_label",
    )
    seams = {name: Mock(name=name) for name in seam_names}
    for name, callback in seams.items():
        setattr(orchestrator, name, callback)
    punctuation = Mock(return_value=False)
    orchestrator._punctuation_enabled = punctuation
    chapter_glossary = Mock(name="extract_chapter_glossary")
    backtranslation = Mock(name="backtranslation_check")
    orchestrator.extractor = SimpleNamespace(extract_and_store=chapter_glossary)
    orchestrator.backtrans = SimpleNamespace(check=backtranslation)

    with patch(
        "trans_novel.pipeline.orchestrator.translate_legacy_chapter",
        return_value=8,
    ) as delegate:
        result = orchestrator._translate_chapter(
            2,
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            "style",
            "synopsis",
            translation_history={},
            source_corpus="corpus",
            annotation_context_registry=None,
            done=5,
            total=10,
        )

    assert result == 8
    call = delegate.call_args
    assert call.kwargs["process_batch"] is seams["_process_batch"]
    assert call.kwargs["term_snapshot"] is seams["_chapter_term_snapshot"]
    assert call.kwargs["extract_batch_glossary"] is seams["_extract_batch_glossary"]
    assert call.kwargs["align_after_batch"] is seams["_align_annotations_after_batch"]
    assert call.kwargs["sync_context_chapter_prefix"] is seams["_sync_context_chapter_prefix"]
    assert call.kwargs["update_translation_history"] is seams["_update_translation_history"]
    assert (
        call.kwargs["annotation_contexts_for_segments"]
        is seams["_annotation_contexts_for_segments"]
    )
    assert call.kwargs["chapter_progress_label"] is seams["_chapter_progress_label"]
    assert call.kwargs["extract_chapter_glossary"] is chapter_glossary
    assert call.kwargs["backtranslation_check"] is backtranslation

    # 配置开关不在 façade 入口冻结；旧代码在原调用位置仍观察到最新值。
    assert call.kwargs["polish_enabled"]() is True
    pipeline.polish = False
    pipeline.rolling_context_segments = 21
    assert call.kwargs["polish_enabled"]() is False
    assert call.kwargs["punctuation_enabled"] is punctuation
    assert call.kwargs["rolling_context_segments"]() == 21
    assert call.kwargs["plan_batches"].__name__ == "_resume_batches"
    assert call.kwargs["report_progress"].__name__ == "_report_translation_progress"
    assert call.kwargs["policy"] == TranslationPolicy(max_chars_per_batch=99)


def test_process_batch_facade_injects_current_agents_config_and_random_sampler() -> None:
    """旧 ``_process_batch`` monkeypatch 点仍按调用时配置委托纯批次函数。"""
    orchestrator = object.__new__(Orchestrator)
    translate = Mock(name="translate_batch")
    polish = Mock(name="polish_batch")
    orchestrator.translator = SimpleNamespace(translate_batch=translate)
    orchestrator.polisher = SimpleNamespace(polish=polish)
    orchestrator.config = SimpleNamespace(
        pipeline=SimpleNamespace(polish=True, backtranslate_sample=0.4)
    )
    random_sample = Mock(name="random_sample")
    expected = BatchResult(targets=["译文"])

    with (
        patch(
            "trans_novel.pipeline.orchestrator.process_legacy_batch",
            return_value=expected,
        ) as delegate,
        patch("trans_novel.pipeline.orchestrator.random.random", random_sample),
    ):
        actual = orchestrator._process_batch(
            [Segment(index=0, source="原文")],
            [],
            "context",
            "style",
            "synopsis",
            "digest",
            annotation_contexts=[[]],
        )

    assert actual is expected
    injected = delegate.call_args.kwargs
    assert injected["translate_batch"] is translate
    assert injected["polish_batch"] is polish
    assert injected["random_sample"] is random_sample
    assert injected["polish_enabled"]() is True
    assert injected["backtranslate_sample"]() == 0.4

    # 旧实现分别在翻译后和润色后取值；配置被回调更新时不得使用入口快照。
    orchestrator.config.pipeline.polish = False
    orchestrator.config.pipeline.backtranslate_sample = 0.9
    assert injected["polish_enabled"]() is False
    assert injected["backtranslate_sample"]() == 0.9


def test_new_batch_preserves_all_durable_side_effect_boundaries() -> None:
    """新批次必须先保存正文，再推进注释、上下文、事件、术语和章状态。"""
    actions: list[str] = []
    chapter = Chapter(index=0, title="第一章", segments=[Segment(index=0, source="原文")])
    store = _StoreSpy(chapter, actions)
    context = _ContextSpy(actions)
    callbacks = _coordinator_callbacks(actions)

    def progress(done: int, _total: int, _label: str) -> None:
        actions.append(f"progress:{done}")

    result = translate_legacy_chapter(
        0,
        store,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        context,  # type: ignore[arg-type]
        "style",
        policy=_policy(),
        translation_history={},
        source_corpus="原文",
        annotation_context_registry=None,
        progress=progress,
        done=0,
        total=1,
        **callbacks,
    )

    assert result == 1
    assert actions == [
        "annotation_contexts",
        "progress:0",
        "term_snapshot",
        "render:7",
        "process_batch",
        "save_chapter",
        "align_after_batch",
        "context_add",
        "sync_context",
        "event:batch_translated",
        "progress:1",
        "extract_batch_glossary",
        "update_history",
        "term_snapshot",
        "extract_chapter_glossary",
        "event:chapter_glossary_extracted",
        "backtranslation_check",
        "event:chapter_backtranslation_checked",
        "save_chapter_with_status:done",
        "event:chapter_done",
    ]
    assert chapter.meta["backtranslation_issues"] == [{"type": "semantic", "chapter": 0}]


def test_resumed_batch_rebuilds_annotation_and_context_without_repeating_checkpoint() -> None:
    """有术语检查点的已译批次只重建派生状态，不再调用批次模型或写 history。"""
    actions: list[str] = []
    chapter = Chapter(
        index=0,
        title="第一章",
        segments=[Segment(index=0, source="原文", target="既有译文")],
    )
    store = _StoreSpy(chapter, actions, checkpoints={"0:1"})
    context = _ContextSpy(actions)
    callbacks = _coordinator_callbacks(actions)

    # 已译批次不会产生本次运行的回译样本，因此测试中回译回调不应触发。
    translate_legacy_chapter(
        0,
        store,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        context,  # type: ignore[arg-type]
        "style",
        policy=_policy(),
        translation_history={},
        source_corpus="原文",
        annotation_context_registry=None,
        done=1,
        total=1,
        **callbacks,
    )

    assert actions == [
        "annotation_contexts",
        "term_snapshot",
        "align_after_batch",
        "context_add",
        "sync_context",
        "term_snapshot",
        "event:batch_skipped",
        "extract_chapter_glossary",
        "event:chapter_glossary_extracted",
        "save_chapter_with_status:done",
        "event:chapter_done",
    ]
    skipped = next(attributes for event, attributes in store.events if event == "batch_skipped")
    assert skipped["glossary_extraction"] == {
        "inserted": 0,
        "conflict": 0,
        "unchanged": 0,
        "updated": 0,
        "skipped": 1,
    }
    assert "process_batch" not in actions
    assert "extract_batch_glossary" not in actions
    assert "update_history" not in actions


def test_chapter_save_failure_cannot_advance_any_dependent_side_effect() -> None:
    """正文首写失败时不得产生注释、上下文、成功事件或术语领先状态。"""
    actions: list[str] = []
    chapter = Chapter(index=0, segments=[Segment(index=0, source="原文")])
    store = _StoreSpy(chapter, actions, fail_save=True)
    callbacks = _coordinator_callbacks(actions)

    with pytest.raises(OSError, match="chapter write failed"):
        translate_legacy_chapter(
            0,
            store,  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            _ContextSpy(actions),  # type: ignore[arg-type]
            "style",
            policy=_policy(),
            translation_history={},
            source_corpus="原文",
            annotation_context_registry=None,
            **callbacks,
        )

    assert actions == [
        "annotation_contexts",
        "term_snapshot",
        "render:7",
        "process_batch",
        "save_chapter",
    ]


def test_legacy_import_does_not_load_new_translation_or_langgraph_runtime() -> None:
    """导入旧编排器不得隐式导入新版 phase、graph 或 LangGraph。"""
    code = r"""
import sys
import trans_novel.pipeline.orchestrator

forbidden = (
    "trans_novel.application.workflow_execution",
    "trans_novel.application.workflow_preparation",
    "trans_novel.graph",
    "langgraph",
)
loaded = [
    name
    for name in sys.modules
    if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
]
if loaded:
    raise SystemExit("unexpected imports: " + ", ".join(sorted(loaded)))
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
