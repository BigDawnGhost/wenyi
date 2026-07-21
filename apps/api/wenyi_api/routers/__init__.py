"""API 路由聚合。"""

from . import (
    chapters,
    events,
    export,
    glossary,
    health,
    projects,
    review,
    strategies,
    style,
    ws,
)

__all__ = [
    "health", "projects", "chapters", "glossary", "review", "style",
    "export", "strategies", "events", "ws",
]
