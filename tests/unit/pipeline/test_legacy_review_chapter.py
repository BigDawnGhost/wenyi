"""旧版章节 Review 叶块拆分后的恢复、排序与依赖边界测试。"""

from __future__ import annotations

import copy
import subprocess
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from trans_novel.agents.reviewer import ReviewOutputError
from trans_novel.ingest.models import Segment
from trans_novel.pipeline import review_chapter as review_chapter_module
from trans_novel.pipeline.review_chapter import review_legacy_chapter


def _config(
    *,
    max_chars_per_batch: int = 100,
    concurrency: int = 1,
    retries: int = 0,
) -> SimpleNamespace:
    """构造叶块执行器实际读取的最小旧版配置视图。"""
    return SimpleNamespace(
        segment=SimpleNamespace(max_chars_per_batch=max_chars_per_batch),
        pipeline=SimpleNamespace(
            review_concurrency=concurrency,
            review_output_retries=retries,
            review_agent_loop=False,
        ),
    )


def _issue(detail: str = "issue") -> dict[str, Any]:
    """返回一个合法块内问题，便于断言章节索引映射。"""
    return {
        "index": 0,
        "type": "mistranslation",
        "detail": detail,
        "suggestion": "revise",
    }


class _Reviewer:
    """把测试脚本映射为旧 Reviewer 的 ``review_result`` 接口。"""

    def __init__(self, handler) -> None:
        self._handler = handler
        self.calls: list[tuple[list[str], list[str], object]] = []

    def review_result(self, sources, targets, terms, *, trace=None):
        """记录不可变调用快照，并执行当前测试指定的结果或异常。"""
        self.calls.append((list(sources), list(targets), terms))
        issues, repaired = self._handler(list(sources), list(targets))
        return SimpleNamespace(issues=issues, repaired=repaired)


class _DebugSpy:
    """记录叶块写入，不依赖真实 ReviewRunStore 或文件系统。"""

    def __init__(self) -> None:
        self.writes: list[tuple[str, object]] = []
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.initial: list[dict[str, Any]] = []
        self.dismissed: list[dict[str, Any]] = []

    def write_json(self, path: str, data: object) -> str:
        """保存深拷贝，避免后续 trace 原地更新污染先前断言。"""
        self.writes.append((path, copy.deepcopy(data)))
        return path

    def log_event(self, event: str, **data: Any) -> None:
        """按实际调用顺序保存结构化事件。"""
        self.events.append((event, copy.deepcopy(data)))

    def record_initial_issues(self, **data: Any) -> None:
        """保存成功叶块的初审问题快照。"""
        self.initial.append(copy.deepcopy(data))

    def record_dismissed(self, **data: Any) -> None:
        """保存 Agent Loop 驳回快照；本组基础测试通常为空。"""
        self.dismissed.append(copy.deepcopy(data))


class _ImmediateFuture:
    """为确定性完成顺序测试提供最小 Future。"""

    def __init__(self, value: object) -> None:
        self._value = value

    def result(self) -> object:
        """返回同步计算完成的 worker 结果。"""
        return self._value


class _InlineExecutor:
    """同步执行 submit，让测试只控制 ``as_completed`` 的观察顺序。"""

    def __init__(self, *, max_workers: int) -> None:
        self.max_workers = max_workers

    def __enter__(self) -> _InlineExecutor:
        """匹配 ThreadPoolExecutor 上下文协议。"""
        return self

    def __exit__(self, *_args: object) -> None:
        """同步 executor 无需清理资源。"""

    def submit(self, function, *args: object) -> _ImmediateFuture:
        """立即执行 job，并把结果封装为可哈希 Future。"""
        return _ImmediateFuture(function(*args))


def test_parallel_completion_keeps_results_in_job_order(monkeypatch) -> None:
    """worker 倒序完成时，问题仍按原块顺序合并，进度按完成顺序上报。"""
    segments = [
        Segment(index=0, source="aaa", target="A"),
        Segment(index=1, source="bbb", target="B"),
        Segment(index=2, source="ccccccc", target="C"),
    ]
    reviewer = _Reviewer(lambda sources, _targets: ([_issue(sources[0])], False))
    progress: list[int] = []

    # 预算为 6，计划块大小为 2、1；倒序 Future 模拟第二块先完成。
    monkeypatch.setattr(review_chapter_module, "ThreadPoolExecutor", _InlineExecutor)
    monkeypatch.setattr(
        review_chapter_module,
        "as_completed",
        lambda futures: reversed(list(futures)),
    )

    issues = review_legacy_chapter(
        segments,
        [],
        config=_config(max_chars_per_batch=2, concurrency=2),  # type: ignore[arg-type]
        client=object(),  # type: ignore[arg-type]
        reviewer=reviewer,  # type: ignore[arg-type]
        recoverable_error=ReviewOutputError,
        chapter_index=7,
        on_chunk_finished=progress.append,
    )

    assert [issue["index"] for issue in issues] == [0, 2]
    assert [issue["detail"] for issue in issues] == ["aaa", "ccccccc"]
    assert progress == [1, 2]


def test_protocol_error_splits_then_retries_singleton_and_flushes_in_order() -> None:
    """畸形多段输出先拆半，单段重试成功后只推进一次顶层块进度。"""
    singleton_attempts = 0

    def handler(sources: list[str], _targets: list[str]):
        nonlocal singleton_attempts
        if len(sources) > 1:
            raise ReviewOutputError("malformed_json")
        if sources == ["left"]:
            singleton_attempts += 1
            if singleton_attempts == 1:
                raise ReviewOutputError("invalid_issue_index")
        return [_issue(sources[0])], False

    reviewer = _Reviewer(handler)
    debug = _DebugSpy()
    progress: list[int] = []

    issues = review_legacy_chapter(
        [
            Segment(index=0, source="left", target="L"),
            Segment(index=1, source="right", target="R"),
        ],
        [],
        config=_config(retries=1),  # type: ignore[arg-type]
        client=object(),  # type: ignore[arg-type]
        reviewer=reviewer,  # type: ignore[arg-type]
        recoverable_error=ReviewOutputError,
        chapter_index=3,
        debug=debug,  # type: ignore[arg-type]
        on_chunk_finished=progress.append,
    )

    assert [call[0] for call in reviewer.calls] == [
        ["left", "right"],
        ["left"],
        ["left"],
        ["right"],
    ]
    assert [issue["index"] for issue in issues] == [0, 1]
    assert progress == [2]
    recovery_names = [
        event
        for event, _data in debug.events
        if event.startswith("review_chunk_") or event.startswith("review_singleton_")
    ]
    assert recovery_names == [
        "review_chunk_split",
        "review_singleton_retry",
        "review_singleton_recovered",
    ]


def test_non_protocol_failure_is_not_split_or_retried() -> None:
    """服务异常必须立即上抛，不能被协议恢复放大为额外模型调用。"""

    def fail(_sources: list[str], _targets: list[str]):
        raise RuntimeError("service unavailable")

    reviewer = _Reviewer(fail)
    debug = _DebugSpy()
    progress: list[int] = []

    with pytest.raises(RuntimeError, match="service unavailable"):
        review_legacy_chapter(
            [
                Segment(index=0, source="left", target="L"),
                Segment(index=1, source="right", target="R"),
            ],
            [],
            config=_config(retries=3),  # type: ignore[arg-type]
            client=object(),  # type: ignore[arg-type]
            reviewer=reviewer,  # type: ignore[arg-type]
            recoverable_error=ReviewOutputError,
            chapter_index=1,
            debug=debug,  # type: ignore[arg-type]
            on_chunk_finished=progress.append,
        )

    assert len(reviewer.calls) == 1
    assert progress == []
    assert not any(
        event.startswith("review_chunk_") or event.startswith("review_singleton_")
        for event, _data in debug.events
    )


def test_failure_after_split_still_flushes_prior_recovery_event() -> None:
    """递归恢复途中遇到服务异常时，finally 仍应留下已经发生的拆分证据。"""

    def handler(sources: list[str], _targets: list[str]):
        if len(sources) > 1:
            raise ReviewOutputError("malformed_json")
        if sources == ["right"]:
            raise RuntimeError("right leaf failed")
        return [], False

    debug = _DebugSpy()
    with pytest.raises(RuntimeError, match="right leaf failed"):
        review_legacy_chapter(
            [
                Segment(index=0, source="left", target="L"),
                Segment(index=1, source="right", target="R"),
            ],
            [],
            config=_config(),  # type: ignore[arg-type]
            client=object(),  # type: ignore[arg-type]
            reviewer=_Reviewer(handler),  # type: ignore[arg-type]
            recoverable_error=ReviewOutputError,
            chapter_index=5,
            debug=debug,  # type: ignore[arg-type]
        )

    split_events = [data for event, data in debug.events if event == "review_chunk_split"]
    assert split_events == [
        {
            "chapter": 5,
            "start_index": 0,
            "count": 2,
            "left_count": 1,
            "right_count": 1,
            "reason": "malformed_json",
        }
    ]


def test_shadow_override_is_read_without_mutating_formal_segment() -> None:
    """盲审轮读取 shadow target，但正式段落实例始终保持只读。"""
    segment = Segment(index=0, source="source", target="formal")
    reviewer = _Reviewer(lambda _sources, _targets: ([_issue()], False))

    issues = review_legacy_chapter(
        [segment],
        [],
        config=_config(),  # type: ignore[arg-type]
        client=object(),  # type: ignore[arg-type]
        reviewer=reviewer,  # type: ignore[arg-type]
        recoverable_error=ReviewOutputError,
        chapter_index=4,
        target_overrides={(4, 0): "shadow"},
        review_round=2,
    )

    assert reviewer.calls[0][1] == ["shadow"]
    assert segment.target == "formal"
    assert issues[0]["_chunk_id"] == "r2-ch4-base0-n1"


def test_empty_chapter_is_a_true_short_path() -> None:
    """空章节不创建 worker、不调用 Reviewer，也不产生进度副作用。"""
    reviewer = _Reviewer(lambda _sources, _targets: ([], False))
    progress: list[int] = []

    assert (
        review_legacy_chapter(
            [],
            [],
            config=_config(concurrency=4),  # type: ignore[arg-type]
            client=object(),  # type: ignore[arg-type]
            reviewer=reviewer,  # type: ignore[arg-type]
            recoverable_error=ReviewOutputError,
            on_chunk_finished=progress.append,
        )
        == []
    )
    assert reviewer.calls == []
    assert progress == []


def test_legacy_leaf_module_does_not_import_new_runtime_or_book_store() -> None:
    """旧版叶块模块不得把 workflow、LangGraph、RunStore 或 usage 带入进程。"""
    script = """
import sys
import trans_novel.pipeline.review_chapter

forbidden = (
    "langgraph",
    "trans_novel.graph",
    "trans_novel.workflow",
    "trans_novel.pipeline.runstore",
    "trans_novel.llm.usage",
)
loaded = sorted(
    name
    for name in sys.modules
    if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
)
if loaded:
    raise SystemExit("forbidden imports: " + ", ".join(loaded))
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
