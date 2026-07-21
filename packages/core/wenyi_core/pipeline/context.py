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
        """追加非空译文，并只保留配置允许的最近尾段。"""
        self.recent_targets.extend(t for t in targets if t and t.strip())
        if len(self.recent_targets) > self.max_recent_keep:
            self.recent_targets = self.recent_targets[-self.max_recent_keep:]

    def to_dict(self) -> dict:
        """序列化滚动上下文及其保留上限。"""
        return {
            "recent_targets": self.recent_targets,
            "max_recent_keep": self.max_recent_keep,
        }

    @classmethod
    def from_dict(
        cls,
        d: dict,
        *,
        min_recent_keep: int = 0,
    ) -> "RollingContext":
        """从持久化字典恢复上下文，并保证至少满足当前配置容量。"""
        persisted = d.get("max_recent_keep", 40)
        max_recent_keep = persisted if isinstance(persisted, int) else 40
        max_recent_keep = max(max_recent_keep, min_recent_keep)
        recent_targets = d.get("recent_targets", []) or []
        return cls(
            recent_targets=recent_targets[-max_recent_keep:],
            max_recent_keep=max_recent_keep,
        )
