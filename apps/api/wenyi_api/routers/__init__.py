"""API 路由聚合。"""

from . import (
    chapters,
    configuration,
    events,
    export,
    glossary,
    health,
    projects,
    report,
    retranslation,
    review,
    strategies,
    style,
    subtitles,
    ws,
)

__all__ = [
    "health",
    "projects",
    "chapters",
    "glossary",
    "review",
    "retranslation",
    "style",
    "export",
    "strategies",
    "events",
    "ws",
    "configuration",
    "report",
    "subtitles",
]
