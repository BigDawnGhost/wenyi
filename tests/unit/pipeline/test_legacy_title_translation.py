"""旧版标题翻译拆分后的行为和依赖边界契约。"""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

import pytest

from trans_novel.ingest.models import Chapter, Segment
from trans_novel.pipeline.orchestrator import Orchestrator
from trans_novel.pipeline.title_translation import translate_legacy_titles


class _StoreSpy:
    """用单一时间线记录旧 manifest 的保存与事件副作用。"""

    def __init__(
        self,
        manifest: dict[str, Any],
        chapters: dict[int, Chapter] | None = None,
    ) -> None:
        self.manifest = manifest
        self.chapters = chapters or {}
        self.actions: list[tuple[str, object]] = []

    def load_manifest(self) -> dict[str, Any]:
        """返回旧 RunStore 风格的可变 manifest。"""
        return self.manifest

    def load_chapter(self, chapter_index: int) -> Chapter:
        """按 manifest 索引返回准备好的旧章节。"""
        return self.chapters[chapter_index]

    def save_manifest(self, manifest: dict[str, Any]) -> None:
        """记录标题写回的持久化边界。"""
        assert manifest is self.manifest
        self.actions.append(("save", manifest))

    def log_event(self, event: str, **attributes: object) -> None:
        """记录保存前后可观察事件的严格顺序。"""
        self.actions.append(("event", (event, attributes)))


class _GlossarySpy:
    """为旧协调器提供稳定且无副作用的空术语快照。"""

    @staticmethod
    def all_terms() -> list[object]:
        """返回标题 prompt 可接受的空术语集合。"""
        return []


def _manifest_with_toc(titles: list[str]) -> dict[str, Any]:
    """构造只含独立目录项的最小旧 manifest。"""
    return {
        "chapters": [],
        "meta": {
            "toc_entries": [
                {
                    "entry_id": f"toc:{index}",
                    "title": title,
                }
                for index, title in enumerate(titles)
            ]
        },
    }


def _event_names(store: _StoreSpy) -> list[str]:
    """把副作用时间线压缩为便于核对的保存/事件名称。"""
    names: list[str] = []
    for kind, payload in store.actions:
        if kind == "event":
            event, _attributes = payload
            names.append(f"event:{event}")
        else:
            names.append(kind)
    return names


def test_orchestrator_facade_preserves_dynamic_legacy_dependency_seam() -> None:
    """旧私有入口必须在调用时读取 client 方法和当前语言配置。"""
    orchestrator = object.__new__(Orchestrator)
    complete_json = Mock()
    orchestrator.client = SimpleNamespace(complete_json=complete_json)
    orchestrator.config = SimpleNamespace(source_lang="ja", target_lang="zh-Hans")
    store = object()
    glossary = object()
    progress = Mock()

    # patch 兼容级模块符号，验证旧集成仍可替换标题协调 seam。
    with patch("trans_novel.pipeline.orchestrator.translate_legacy_titles") as delegate:
        orchestrator._translate_titles(store, glossary, progress)  # type: ignore[arg-type]

    delegate.assert_called_once_with(
        store,
        glossary,
        complete_json=complete_json,
        source_lang="ja",
        target_lang="zh-Hans",
        progress=progress,
    )


def test_complete_split_heading_is_reused_and_saved_before_skip_event() -> None:
    """完整 heading 续段应同步到 TOC/章节，且不触发任何模型调用。"""
    manifest = {
        "chapters": [
            {
                "index": 0,
                "title": "Chapter One",
                "toc_entry_id": "toc:0",
            }
        ],
        "meta": {
            "toc_entries": [
                {
                    "entry_id": "toc:0",
                    "title": "Chapter\nOne",
                    "segment_anchor": "heading-1",
                }
            ]
        },
    }
    chapter = Chapter(
        index=0,
        segments=[
            Segment(
                index=0,
                source="Chapter ",
                target="第一",
                kind="heading",
                anchor="heading-1",
            ),
            Segment(
                index=1,
                source="One",
                target="章",
                kind="heading",
                cont=True,
            ),
        ],
    )
    store = _StoreSpy(manifest, {0: chapter})
    complete_json = Mock(side_effect=AssertionError("reused headings must not call the model"))

    translate_legacy_titles(
        store,  # type: ignore[arg-type]
        _GlossarySpy(),  # type: ignore[arg-type]
        complete_json=complete_json,
        source_lang="en",
        target_lang="zh",
    )

    entry = manifest["meta"]["toc_entries"][0]
    assert entry["title_translated"] == "第一章"
    assert manifest["chapters"][0]["title_translated"] == "第一章"
    assert _event_names(store) == ["save", "event:titles_skipped"]
    complete_json.assert_not_called()


@pytest.mark.parametrize(
    ("titles", "expected_batch_sizes"),
    [
        ([f"Title {index}" for index in range(42)], [40, 2]),
        (["a" * 3000, "b" * 1001, "tail"], [1, 2]),
    ],
)
def test_batch_limits_preserve_save_event_and_progress_order(
    titles: list[str],
    expected_batch_sizes: list[int],
) -> None:
    """40 项和 4000 字边界都应逐批保存，再发事件和进度。"""
    store = _StoreSpy(_manifest_with_toc(titles))
    timeline: list[tuple[str, object]] = []
    calls: list[dict[str, object]] = []

    def complete_json(messages, **kwargs):
        """按 numbered prompt 的条目数生成等长结果并记录模型边界。"""
        numbered = messages[1]["content"].split("\n")
        batch_size = sum(1 for line in numbered if line.startswith("[") and "] " in line)
        calls.append({"messages": messages, "kwargs": kwargs, "size": batch_size})
        timeline.append(("call", batch_size))
        return {"titles": [f"译名-{len(calls)}-{index}" for index in range(batch_size)]}

    original_save = store.save_manifest
    original_event = store.log_event

    def save_manifest(manifest: dict[str, Any]) -> None:
        """把实际保存同时投影到跨组件顺序时间线。"""
        original_save(manifest)
        timeline.append(("save", len(calls)))

    def log_event(event: str, **attributes: object) -> None:
        """把实际事件同时投影到跨组件顺序时间线。"""
        original_event(event, **attributes)
        timeline.append(("event", event))

    def progress(done: int, total: int, label: str) -> None:
        """记录初始与逐批进度，确保它们发生在事件之后。"""
        timeline.append(("progress", (done, total, label)))

    store.save_manifest = save_manifest  # type: ignore[method-assign]
    store.log_event = log_event  # type: ignore[method-assign]

    translate_legacy_titles(
        store,  # type: ignore[arg-type]
        _GlossarySpy(),  # type: ignore[arg-type]
        complete_json=complete_json,
        source_lang="en",
        target_lang="zh",
        progress=progress,
    )

    assert [call["size"] for call in calls] == expected_batch_sizes
    assert all(call["kwargs"] == {"tier": "strong", "stage": "title_translate"} for call in calls)
    assert timeline[0] == ("progress", (0, len(titles), "翻译章节标题…"))
    cursor = 1
    completed = 0
    for batch_number, batch_size in enumerate(expected_batch_sizes, start=1):
        completed += batch_size
        assert timeline[cursor : cursor + 4] == [
            ("call", batch_size),
            ("save", batch_number),
            ("event", "titles_translated"),
            ("progress", (completed, len(titles), "翻译章节标题")),
        ]
        cursor += 4


def test_invalid_model_count_logs_rejection_without_saving_partial_state() -> None:
    """数量不匹配必须先记录拒绝事件，再抛错且不写入 manifest。"""
    manifest = _manifest_with_toc(["Only title"])
    store = _StoreSpy(manifest)

    with pytest.raises(RuntimeError, match="invalid number"):
        translate_legacy_titles(
            store,  # type: ignore[arg-type]
            _GlossarySpy(),  # type: ignore[arg-type]
            complete_json=lambda *_args, **_kwargs: {"titles": []},
            source_lang="en",
            target_lang="zh",
        )

    assert _event_names(store) == ["event:titles_translation_rejected"]
    assert "title_translated" not in manifest["meta"]["toc_entries"][0]


def test_model_error_is_logged_before_the_same_exception_escapes() -> None:
    """模型异常必须保持原对象向上抛出，且失败事件中保留旧 repr。"""
    store = _StoreSpy(_manifest_with_toc(["Only title"]))
    failure = LookupError("provider failed")

    def fail(*_args, **_kwargs):
        """模拟标题模型在产生任何译名之前失败。"""
        raise failure

    with pytest.raises(LookupError) as caught:
        translate_legacy_titles(
            store,  # type: ignore[arg-type]
            _GlossarySpy(),  # type: ignore[arg-type]
            complete_json=fail,
            source_lang="en",
            target_lang="zh",
        )

    assert caught.value is failure
    assert _event_names(store) == ["event:titles_translation_failed"]
    event, attributes = store.actions[0][1]
    assert event == "titles_translation_failed"
    assert attributes == {
        "batch": 0,
        "count": 1,
        "error": "LookupError('provider failed')",
    }


def test_rerun_only_translates_items_after_the_last_durable_batch() -> None:
    """中途失败后的重跑必须跳过已保存批次，只补最后的未完成项。"""
    titles = [f"Title {index}" for index in range(41)]
    manifest = _manifest_with_toc(titles)
    store = _StoreSpy(manifest)
    call_number = 0

    def fail_second_batch(messages, **_kwargs):
        """第一批成功，第二批失败，模拟断点续跑边界。"""
        nonlocal call_number
        call_number += 1
        if call_number == 2:
            raise RuntimeError("temporary outage")
        return {"titles": [f"译名-{index}" for index in range(40)]}

    with pytest.raises(RuntimeError, match="temporary outage"):
        translate_legacy_titles(
            store,  # type: ignore[arg-type]
            _GlossarySpy(),  # type: ignore[arg-type]
            complete_json=fail_second_batch,
            source_lang="en",
            target_lang="zh",
        )

    # 第一批已经通过 save_manifest 成为旧任务的持久断点。
    entries = manifest["meta"]["toc_entries"]
    assert all(entry.get("title_translated") for entry in entries[:40])
    assert "title_translated" not in entries[40]
    assert _event_names(store) == [
        "save",
        "event:titles_translated",
        "event:titles_translation_failed",
    ]

    resumed_sources: list[str] = []

    def complete_resume(messages, **_kwargs):
        """记录重跑 prompt，确认其中只有未落盘的最后一项。"""
        resumed_sources.append(messages[1]["content"])
        return {"titles": ["最后译名"]}

    translate_legacy_titles(
        store,  # type: ignore[arg-type]
        _GlossarySpy(),  # type: ignore[arg-type]
        complete_json=complete_resume,
        source_lang="en",
        target_lang="zh",
    )

    assert len(resumed_sources) == 1
    assert "Title 40" in resumed_sources[0]
    assert "Title 39" not in resumed_sources[0]
    assert entries[40]["title_translated"] == "最后译名"


def test_legacy_title_module_imports_without_new_runtime_or_body_translator() -> None:
    """只导入旧标题模块时不得加载新版栈或正文 Translator。"""
    script = """
import sys
import trans_novel.pipeline.title_translation

forbidden = (
    "trans_novel.application",
    "trans_novel.application.workflow_execution",
    "trans_novel.workflow",
    "trans_novel.graph",
    "trans_novel.agents.translator",
    "langgraph",
)
loaded = [name for name in forbidden if name in sys.modules]
if loaded:
    raise SystemExit(f"unexpected title-translation dependencies: {loaded}")
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
