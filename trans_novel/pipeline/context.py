"""滚动上下文：故事梗概（持续更新）+ 前文译文尾段。

注入翻译 prompt，保证跨批次/跨章的连贯与代词指代正确。
梗概更新用廉价档，长度有上限以控成本。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..llm.base import LLMClient

_SUMMARY_SYSTEM = (
    "你是小说情节摘要员。根据【已有梗概】和【新章节译文】，"
    "更新出一段简洁连贯的中文故事梗概（不超过 300 字），"
    "保留关键人物、关系与情节进展，去除细节。只输出梗概正文。"
)


@dataclass
class RollingContext:
    summary: str = ""
    recent_targets: list[str] = field(default_factory=list)
    max_recent_keep: int = 40  # 最多保留多少段尾部译文

    def render(self, n_recent: int) -> str:
        parts: list[str] = []
        if self.summary.strip():
            parts.append("【故事梗概】\n" + self.summary.strip())
        tail = self.recent_targets[-n_recent:] if n_recent > 0 else []
        if tail:
            parts.append("【前文译文（最近）】\n" + "\n".join(tail))
        return "\n\n".join(parts)

    def add_targets(self, targets: list[str]) -> None:
        self.recent_targets.extend(t for t in targets if t and t.strip())
        if len(self.recent_targets) > self.max_recent_keep:
            self.recent_targets = self.recent_targets[-self.max_recent_keep:]

    def update_summary(self, client: LLMClient, chapter_target_text: str) -> None:
        """用廉价档把新章译文并入故事梗概。失败则保持原梗概不变。"""
        user = (
            f"【已有梗概】\n{self.summary or '（无）'}\n\n"
            f"【新章节译文】\n{chapter_target_text[:6000]}"
        )
        try:
            text = client.complete(
                [{"role": "system", "content": _SUMMARY_SYSTEM},
                 {"role": "user", "content": user}],
                tier="cheap",
            )
            if text and text.strip():
                self.summary = text.strip()
        except Exception:
            pass

    def to_dict(self) -> dict:
        return {"summary": self.summary, "recent_targets": self.recent_targets}

    @classmethod
    def from_dict(cls, d: dict) -> "RollingContext":
        return cls(
            summary=d.get("summary", ""),
            recent_targets=d.get("recent_targets", []) or [],
        )
