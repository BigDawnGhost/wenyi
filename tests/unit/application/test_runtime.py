"""框架无关应用 DTO、端口和 invocation 上下文合同测试。"""

from __future__ import annotations

import inspect
import subprocess
import sys

from trans_novel.application.models import (
    ApplicationEvent,
    ProgressUpdate,
    UsageRecord,
)
from trans_novel.application.ports import Clock, EventSink, ProgressSink, RandomSource, UsageSink
from trans_novel.application.runtime import ExecutionContext


class RecordingProgress:
    """保存单测收到的进度事实。"""

    def __init__(self) -> None:
        self.updates: list[ProgressUpdate] = []

    def publish(self, update: ProgressUpdate) -> None:
        self.updates.append(update)


class RecordingEvents:
    """保存单测收到的应用事件。"""

    def __init__(self) -> None:
        self.events: list[ApplicationEvent] = []

    def publish(self, event: ApplicationEvent) -> None:
        self.events.append(event)


class RecordingUsage:
    """保存单测收到的独立用量增量。"""

    def __init__(self) -> None:
        self.records: list[UsageRecord] = []

    def record(self, usage: UsageRecord) -> None:
        self.records.append(usage)


class ManualClock:
    """提供确定性 UTC 和单调时间。"""

    def utc_now_ms(self) -> int:
        return 1_700_000_000_000

    def monotonic(self) -> float:
        return 12.5


class FixedRandom:
    """提供确定性随机样本。"""

    def random(self) -> float:
        return 0.25


def test_application_import_has_no_legacy_runtime_or_framework_dependencies() -> None:
    """干净解释器导入基础层不得加载旧执行器、存储、LLM 或外部模型框架。"""
    script = """
import sys
import trans_novel.application.models
import trans_novel.application.ports
import trans_novel.application.runtime

forbidden = (
    "trans_novel.cli",
    "trans_novel.config",
    "trans_novel.llm",
    "trans_novel.pipeline",
    "trans_novel.pipeline.orchestrator",
    "trans_novel.pipeline.runstore",
    "trans_novel.storage",
    "langgraph",
    "pydantic",
)
loaded = [name for name in forbidden if name in sys.modules]
if loaded:
    raise SystemExit(f"unexpected application dependencies: {loaded}")
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_execution_context_routes_only_invocation_owned_observations() -> None:
    progress = RecordingProgress()
    events = RecordingEvents()
    usage = RecordingUsage()
    context = ExecutionContext(
        run_id="run-1",
        progress=progress,
        events=events,
        usage=usage,
        clock=ManualClock(),
        random=FixedRandom(),
    )
    update = ProgressUpdate(stage="prepare", completed=1, total=2, message="parsed")
    event = ApplicationEvent(name="pipeline.started", attributes=(("step", "prepare"),))
    record = UsageRecord(stage="prepare", provider="fake", model="fixed", prompt_tokens=3)

    context.progress.publish(update)
    context.events.publish(event)
    context.usage.record(record)

    assert progress.updates == [update]
    assert events.events == [event]
    assert usage.records == [record]
    assert context.clock.utc_now_ms() == 1_700_000_000_000
    assert context.clock.monotonic() == 12.5
    assert context.random.random() == 0.25


def test_default_execution_context_does_not_share_invocation_adapters() -> None:
    first = ExecutionContext(run_id="run-1")
    second = ExecutionContext(run_id="run-2")

    assert first.progress is not second.progress
    assert first.events is not second.events
    assert first.usage is not second.usage
    assert first.clock is not second.clock
    assert first.random is not second.random


def test_ports_are_narrow_structural_protocols() -> None:
    assert inspect.isclass(ProgressSink)
    assert set(ProgressSink.__dict__) & {"publish"} == {"publish"}
    assert set(EventSink.__dict__) & {"publish"} == {"publish"}
    assert set(UsageSink.__dict__) & {"record"} == {"record"}
    assert set(Clock.__dict__) & {"utc_now_ms", "monotonic"} == {"utc_now_ms", "monotonic"}
    assert set(RandomSource.__dict__) & {"random"} == {"random"}


def test_public_application_contracts_are_documented() -> None:
    contracts = (
        ApplicationEvent,
        ProgressUpdate,
        UsageRecord,
        ProgressSink,
        EventSink,
        UsageSink,
        Clock,
        RandomSource,
        ExecutionContext,
    )

    assert all(inspect.getdoc(contract) for contract in contracts)
