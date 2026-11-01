"""PostgreSQL repository and export ports, owned by one Web application."""

import re
import shutil
import time
from pathlib import Path

from psycopg.types.json import Jsonb
from wenyi_backend.context import BackendContext
from wenyi_backend.errors import PROJECT_BUSY, PROJECT_NOT_FOUND, ApiError
from wenyi_backend.export_paths import EXPORT_LIMIT
from wenyi_backend.metrics import Metrics
from wenyi_core.llm.factory import build_client

from . import dal
from .config import Settings
from .db import init_pool
from .export_retention import (
    export_history_lock,
    export_history_session_lock,
    open_export,
    publish_export,
)
from .global_settings import PostgresSettings
from .metrics import RedisCounters
from .storage_pg import PostgresStorage
from .telemetry import RedisTelemetry


class PostgresRepository:
    """Explicit adapter surface; SQL helpers retain their typed call signatures."""

    create_project = staticmethod(dal.create_project)
    get_project = staticmethod(dal.get_project)
    list_projects = staticmethod(dal.list_projects)
    delete_project = staticmethod(dal.delete_project)
    set_project_status = staticmethod(dal.set_project_status)
    request_pause = staticmethod(dal.request_pause)
    set_project_strategy = staticmethod(dal.set_project_strategy)
    set_project_config = staticmethod(dal.set_project_config)
    set_project_languages = staticmethod(dal.set_project_languages)
    set_project_source = staticmethod(dal.set_project_source)
    chapter_summaries = staticmethod(dal.chapter_summaries)
    total_word_count = staticmethod(dal.total_word_count)
    create_job = staticmethod(dal.create_job)
    get_job = staticmethod(dal.get_job)
    get_job_by_arq_id = staticmethod(dal.get_job_by_arq_id)
    set_job_status = staticmethod(dal.set_job_status)
    list_jobs = staticmethod(dal.list_jobs)
    latest_resumable_job = staticmethod(dal.latest_resumable_job)
    job_review_id = staticmethod(dal.job_review_id)
    is_paused = staticmethod(dal.is_paused)
    create_export = staticmethod(dal.create_export)
    set_export_status = staticmethod(dal.set_export_status)

    def __init__(self, settings: Settings):
        self.settings = settings
        self._pool = None

    @property
    def pool(self):
        if self._pool is None:
            raise RuntimeError("Web database pool has not started")
        return self._pool

    def start(self):
        self._pool = init_pool(self.settings.psycopg_dsn)

    def close(self):
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    def project_dir(self, pid):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", pid):
            raise ValueError("Invalid project ID")
        return str(Path(self.settings.data_dir) / pid)

    def storage_for(self, pid):
        return PostgresStorage(pid, self.pool, run_dir=self.project_dir(pid))

    def remove_project(self, pid: str) -> None:
        """Delete owned files before committing the cascading catalog deletion."""
        from wenyi_backend.project_service import project_write

        directory = Path(self.project_dir(pid))
        root = Path(self.settings.data_dir).resolve()
        if directory.is_symlink() or directory.resolve().parent != root:
            raise ApiError(PROJECT_BUSY, 409, "Project directory is outside DATA_DIR")
        with (
            project_write(pid, self, self.storage_for),
            export_history_session_lock(self.pool, pid) as connection,
        ):
            row = connection.execute(
                """SELECT EXISTS (
                    SELECT 1 FROM jobs WHERE project_id=%s AND status IN ('queued','running')
                    UNION ALL
                    SELECT 1 FROM exports WHERE project_id=%s AND status IN ('pending','running')
                )""",
                (pid, pid),
            ).fetchone()
            connection.commit()
            if row and row[0]:
                raise ApiError(PROJECT_BUSY, 409, "A task is active; wait for it before deleting")
            try:
                if directory.exists():
                    shutil.rmtree(directory)
            except OSError as error:
                raise ApiError(
                    PROJECT_BUSY,
                    409,
                    "Could not remove project files; close files using this project and retry",
                ) from error
            dal.delete_project(pid, connection=connection)
            connection.commit()

    def health(self):
        with self.pool.connection() as conn:
            conn.execute("SELECT 1")

    def queued_job_count(self) -> int:
        with self.pool.connection() as connection:
            row = connection.execute("SELECT count(*) FROM jobs WHERE status='queued'").fetchone()
            return row[0]

    def admit_job(self, pid, kind, job_id, *, project_status, **kwargs):
        result = dal.create_job(pid, kind, job_id, status="queued", **kwargs)
        dal.set_project_status(pid, project_status)
        return result

    def fail_admission(self, job_id, status, error):
        job = dal.get_job(job_id)
        dal.set_job_status(job_id, "error", error=error)
        if job:
            dal.set_project_status(job["project_id"], status)

    def update_term(self, pid, storage, source, term):
        with self.pool.connection() as conn:
            conn.execute(
                """UPDATE glossary SET source=%s,target=%s,reading=%s,type=%s,gender=%s,
                aliases=%s,note=%s,status='ok',updated_at=%s WHERE project_id=%s AND source=%s""",
                (
                    term.source,
                    term.target,
                    term.reading,
                    term.type,
                    term.gender,
                    Jsonb(term.aliases),
                    term.note,
                    time.time(),
                    pid,
                    source,
                ),
            )
            conn.execute(
                "UPDATE term_conflicts SET source=%s,resolved=TRUE WHERE project_id=%s AND source=%s",
                (term.source, pid, source),
            )


class PostgresExports:
    def __init__(self, repository):
        self.repository = repository

    def list_exports(self, pid):
        with self.repository.pool.connection() as connection:
            rows = connection.execute(
                """SELECT id,project_id,format,status,path,size,created_at,options,error FROM exports
                   WHERE project_id=%s ORDER BY COALESCE(completed_at,created_at) DESC,id DESC
                   LIMIT %s""",
                (pid, EXPORT_LIMIT),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(
                zip(
                    (
                        "id",
                        "project_id",
                        "format",
                        "status",
                        "path",
                        "size",
                        "created_at",
                        "options",
                        "error",
                    ),
                    row,
                )
            )
            item["created_at"] = item["created_at"].isoformat() if item["created_at"] else None
            result.append(item)
        return result

    def admit_export(self, pid, fmt, options, run_id, snapshot):
        with self.repository.pool.connection() as connection:
            export_history_lock(connection, pid)
            if connection.execute("SELECT 1 FROM projects WHERE id=%s", (pid,)).fetchone() is None:
                raise ApiError(PROJECT_NOT_FOUND, 404, "project not found")
            export_id = dal.create_export(pid, fmt, options, connection=connection)
            job_id = dal.create_job(
                pid,
                "export",
                run_id,
                run_id=run_id,
                params={"export_id": export_id},
                config_snapshot=snapshot,
                connection=connection,
            )
        return export_id, job_id

    def fail_export_admission(self, job_id, error):
        job = dal.get_job(job_id)
        dal.set_job_status(job_id, "error", error=error)
        if job:
            dal.set_export_status(job["params"]["export_id"], "error", error=error)

    def open_export(self, pid, export_id):
        return open_export(
            self.repository.pool, pid, export_id, data_dir=self.repository.settings.data_dir
        )

    def publish_export(self, pid, export_id, output):
        return publish_export(
            self.repository.pool, pid, export_id, output, data_dir=self.repository.settings.data_dir
        )


def postgres_repository(context: BackendContext) -> PostgresRepository:
    """Narrow platform capabilities without adding PostgreSQL to the shared port."""
    repository = context.repository
    assert isinstance(repository, PostgresRepository)
    return repository


def create_context(settings: Settings) -> BackendContext:
    from .queue import enqueue

    repository = PostgresRepository(settings)

    async def submit(name, **kwargs):
        return await enqueue(settings.redis_url, name, **kwargs)

    return BackendContext(
        repository=repository,
        settings_store=PostgresSettings(repository, settings.config_path),
        exports=PostgresExports(repository),
        telemetry=RedisTelemetry(settings.redis_url, repository),
        storage_for=repository.storage_for,
        read_storage_for=repository.storage_for,
        build_client=build_client,
        enqueue=submit,
        data_dir=settings.data_dir,
        project_dir=repository.project_dir,
        health=repository.health,
        update_term=repository.update_term,
        api_token=settings.api_token,
        remove_project=repository.remove_project,
        metrics=Metrics(RedisCounters(settings.redis_url)),
    )
