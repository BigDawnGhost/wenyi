"""Repository access in the current request or worker scope."""

from .context import current_context

RUNNING_PROJECT_STATUSES = frozenset(
    {"preparing", "translating", "reviewing", "translating_subtitles", "parsing", "pausing"}
)


def __getattr__(name: str):
    if name.startswith("_"):
        raise AttributeError(name)

    def query(*args, **kwargs):
        return getattr(current_context().repository, name)(*args, **kwargs)

    return query
