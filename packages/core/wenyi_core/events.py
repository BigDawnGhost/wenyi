"""进度事件总线抽象（tech-stack §7）。

内核通过现有的 ``ProgressFn`` 回调（``orchestrator.run(progress=cb)``）汇报进度，
与 UI 无关。本模块定义 :class:`ProgressEmitter` Protocol，供"桥接层"把
``ProgressFn`` 调用转换为可推送的事件：

- CLI：``NullEmitter``（进度走 rich 进度条，emitter 空实现）
- Web：``RedisEmitter``（实现在 apps/api，发布到 Redis Pub/Sub → WebSocket）

内核本身不依赖 Redis；它只调用注入的 ``progress`` 回调。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable


@dataclass
class TranslationEvent:
    """一次进度事件。

    对齐 PRD/tech-stack 的事件类型：``prepare.step_done``、``chapter.batch_done``、
    ``term.added``、``chapter.completed``、``pipeline.completed`` 等。MVP 阶段
    用 ``kind`` 粗粒度分类，``label`` 承载可展示文本（兼容现有 ProgressFn 的 label）。
    """

    project_id: Optional[str] = None
    kind: str = "progress"          # progress | batch | chapter | term | pipeline | log
    done: int = 0
    total: int = 0
    label: str = ""
    payload: Optional[dict] = None


@runtime_checkable
class ProgressEmitter(Protocol):
    """进度事件发射器接口。"""

    def emit(self, event: TranslationEvent) -> None: ...


class NullEmitter:
    """空发射器：CLI 本地模式用（进度由 rich 直接渲染，无需转发）。"""

    def emit(self, event: TranslationEvent) -> None:  # noqa: D401
        return None


def make_progress_fn(emitter: ProgressEmitter,
                     project_id: Optional[str] = None,
                     *,
                     kind: str = "progress"):
    """把一个 :class:`ProgressEmitter` 包装成内核所需的 ``ProgressFn``。

    内核签名：``progress(done: int, total: int, label: str) -> None``。
    """
    def fn(done: int, total: int, label: str) -> None:
        emitter.emit(TranslationEvent(
            project_id=project_id, kind=kind,
            done=done, total=total, label=label,
        ))
    return fn
