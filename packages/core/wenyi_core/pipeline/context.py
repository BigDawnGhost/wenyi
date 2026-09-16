"""Recent translated paragraphs for local continuity of pronouns, address and tone.
Source prescans provide a fixed whole-book synopsis and chapter digest as stable prompt
prefixes. This module manages only the recent-translation suffix that changes each batch,
complementing that global context.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RollingContext:
    recent_targets: list[str] = field(default_factory=list)
    max_recent_keep: int = 40  # Maximum number of recent translated paragraphs to retain.

    def render(self, n_recent: int) -> str:
        """Return the last n_recent translations as text; the template supplies its context
        heading.
        """
        tail = self.recent_targets[-n_recent:] if n_recent > 0 else []
        return "\n".join(tail)

    def add_targets(self, targets: list[str]) -> None:
        """Append nonempty translations and retain only the configured tail."""
        self.recent_targets.extend(t for t in targets if t and t.strip())
        if len(self.recent_targets) > self.max_recent_keep:
            self.recent_targets = self.recent_targets[-self.max_recent_keep :]

    def to_dict(self) -> dict:
        """Serialize recent translations and their retention limit."""
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
        """Restore persisted context with at least the capacity required by current
        configuration.
        """
        persisted = d.get("max_recent_keep", 40)
        max_recent_keep = persisted if isinstance(persisted, int) else 40
        max_recent_keep = max(max_recent_keep, min_recent_keep)
        recent_targets = d.get("recent_targets", []) or []
        return cls(
            recent_targets=recent_targets[-max_recent_keep:],
            max_recent_keep=max_recent_keep,
        )
