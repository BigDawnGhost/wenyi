"""滚动上下文：最近若干段译文尾巴，供局部连贯（代词指代/称谓/语气衔接）。

全局/前瞻理解改由翻译前的源文预扫提供（见 agents/synopsis.py）：
【全书概览】（全程恒定）+【本章梗概】（每章恒定）作为稳定前缀注入翻译 prompt，
本模块只负责"最近译文"这段每批变化的局部尾巴，二者互补。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RollingContext:
    recent_targets: list[str] = field(default_factory=list)
    max_recent_keep: int = 40  # 最多保留多少段尾部译文

    def render(self, n_recent: int) -> str:
        """返回最近 n_recent 段译文（纯文本，模板自带【前文译文】标题）。"""
        tail = self.recent_targets[-n_recent:] if n_recent > 0 else []
        return "\n".join(tail)

    def add_targets(self, targets: list[str]) -> None:
        self.recent_targets.extend(t for t in targets if t and t.strip())
        if len(self.recent_targets) > self.max_recent_keep:
            self.recent_targets = self.recent_targets[-self.max_recent_keep:]

    def to_dict(self) -> dict:
        return {"recent_targets": self.recent_targets}

    @classmethod
    def from_dict(cls, d: dict) -> "RollingContext":
        return cls(recent_targets=d.get("recent_targets", []) or [])
