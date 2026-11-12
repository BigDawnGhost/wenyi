"""Durable job identity needed by workflow and export execution."""

from typing import Any, Protocol


class JobRepository(Protocol):
    def get_job_by_arq_id(self, arq_job_id: str) -> dict | None: ...
    def set_job_status(
        self, job_id: int, status: str, error: str | None = None, *, result: dict | None = None
    ) -> Any: ...
    def list_jobs(self, pid: str) -> list[dict]: ...


class JobAdmission(Protocol):
    """Admission and compensation stay single operations in each platform adapter."""

    def admit_job(
        self,
        pid: str,
        kind: str,
        job_id: str,
        /,
        *,
        project_status: str,
        params: dict | None = None,
        config_snapshot: dict | None = None,
        run_id: str | None = None,
    ) -> int: ...
    def fail_admission(self, job_id: int, previous_status: str, error: str, /) -> Any: ...


class JobCatalog(JobRepository, JobAdmission, Protocol):
    def queued_job_count(self) -> int: ...

    def create_job(
        self,
        pid: str,
        kind: str,
        arq_job_id: str,
        *,
        params: dict | None = None,
        config_snapshot: dict | None = None,
        run_id: str | None = None,
        status: str = "queued",
    ) -> int: ...
    def get_job(self, job_id: int) -> dict | None: ...
    def latest_resumable_job(self, pid: str) -> dict | None: ...
    def job_review_id(self, job_id: int) -> str | None: ...
