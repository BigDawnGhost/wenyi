"""滚动上下文：保留最近若干段译文，供代词指代、称谓和语气衔接。"""

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
            self.recent_targets = self.recent_targets[-self.max_recent_keep :]

    def to_dict(self) -> dict:
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
    ) -> RollingContext:
        persisted = d.get("max_recent_keep", 40)
        max_recent_keep = persisted if isinstance(persisted, int) else 40
        max_recent_keep = max(max_recent_keep, min_recent_keep)
        recent_targets = d.get("recent_targets", []) or []
        return cls(
            recent_targets=recent_targets[-max_recent_keep:],
            max_recent_keep=max_recent_keep,
        )
