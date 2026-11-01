"""Project identity and workflow state needed by execution services."""

from typing import Any, Protocol

RUNNING_PROJECT_STATUSES = frozenset(
    {"preparing", "translating", "reviewing", "translating_subtitles", "parsing", "pausing"}
)


class ProjectReader(Protocol):
    def get_project(self, pid: str) -> dict | None: ...


class ProjectQueries(ProjectReader, Protocol):
    """Read projections needed by source views and live snapshots."""

    def chapter_summaries(self, pid: str) -> list[dict]: ...


class ProjectRepository(ProjectReader, Protocol):
    def set_project_status(self, pid: str, status: str, *, error: str | None = None) -> None: ...
    def is_paused(self, pid: str) -> bool: ...
    def request_pause(self, pid: str, run_id: str) -> bool:
        """Pause only the latest active workflow, atomically with its project-state check."""
        ...


class ProjectCatalog(ProjectRepository, ProjectQueries, Protocol):
    """Project editing and read projections consumed by HTTP application services."""

    def create_project(
        self,
        name: str,
        source_lang: str,
        target_lang: str,
        strategy: dict,
        *,
        project_id: str | None = None,
        source: dict | None = None,
        config: dict | None = None,
        connection: Any = None,
    ) -> str: ...
    def list_projects(self) -> list[dict]: ...
    def delete_project(self, pid: str) -> None: ...
    def set_project_strategy(self, pid: str, strategy: dict, *, connection: Any = None) -> None: ...
    def set_project_config(self, pid: str, config: dict, *, connection: Any = None) -> None: ...
    def set_project_languages(
        self, pid: str, source_lang: str, target_lang: str, *, connection: Any = None
    ) -> None: ...
    def set_project_source(
        self,
        pid: str,
        source_path: str,
        book_title: str | None,
        *,
        source_sha256: str | None = None,
        fmt: str | None = None,
        source_meta: dict | None = None,
    ) -> None: ...
    def total_word_count(self, pid: str) -> int: ...
