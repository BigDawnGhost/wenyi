"""Project queries and statistics used by the API outside the core Storage protocol."""

from __future__ import annotations

import uuid
from contextlib import nullcontext
from datetime import datetime, timezone
from typing import Any, Optional

from psycopg import Connection
from psycopg.types.json import Jsonb

from .db import get_pool

RUNNING_PROJECT_STATUSES = frozenset(
    {
        "preparing",
        "translating",
        "reviewing",
        "translating_subtitles",
        "parsing",
        "pausing",
    }
)


def _conn(connection: Connection[Any] | None = None):
    return nullcontext(connection) if connection is not None else get_pool().connection()


def create_project(
    name: str,
    source_lang: str,
    target_lang: str,
    strategy: dict[str, Any],
    *,
    project_id: str | None = None,
    source: dict | None = None,
    config: dict | None = None,
    connection: Connection[Any] | None = None,
) -> str:
    pid = project_id or uuid.uuid4().hex[:16]
    source = source or {}
    with _conn(connection) as c:
        c.execute(
            """INSERT INTO projects
               (id, name, source_lang, target_lang, status, strategy, source_path,
                book_title, source_sha256, fmt, source_meta, config)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                pid,
                name,
                source_lang,
                target_lang,
                "uploaded" if source else "created",
                Jsonb(strategy),
                source.get("source_path"),
                source.get("book_title"),
                source.get("source_sha256"),
                source.get("fmt"),
                Jsonb(source.get("source_meta", {})),
                Jsonb(config or {}),
            ),
        )
    return pid


def list_projects() -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            """SELECT id, name, title, fmt, source_lang, target_lang, status, created_at
               FROM projects ORDER BY created_at DESC"""
        ).fetchall()
    return [
        {
            "id": r[0],
            "name": r[1],
            "title": r[2],
            "fmt": r[3],
            "source_lang": r[4],
            "target_lang": r[5],
            "status": r[6],
            "created_at": r[7].isoformat() if r[7] else None,
        }
        for r in rows
    ]


def get_project(pid: str) -> Optional[dict]:
    with _conn() as c:
        r = c.execute(
            """SELECT id, name, title, fmt, source_lang, target_lang, status,
                      strategy, book_title, created_at, source_path, config, source_sha256, source_meta, initialized, error
               FROM projects WHERE id=%s""",
            (pid,),
        ).fetchone()
    if r is None:
        return None
    return {
        "id": r[0],
        "name": r[1],
        "title": r[2],
        "fmt": r[3],
        "source_lang": r[4],
        "target_lang": r[5],
        "status": r[6],
        "strategy": r[7],
        "book_title": r[8],
        "created_at": r[9].isoformat() if r[9] else None,
        "source_path": r[10],
        "config": r[11] or {},
        "source_sha256": r[12],
        "source_meta": r[13] or {},
        "initialized": r[14],
        "error": r[15],
    }


def set_project_status(pid: str, status: str, *, error: str | None = None) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE projects SET status=%s, error=%s, updated_at=now() WHERE id=%s",
            (status, error, pid),
        )


def set_project_strategy(
    pid: str, strategy: dict[str, Any], *, connection: Connection[Any] | None = None
) -> None:
    with _conn(connection) as c:
        c.execute(
            "UPDATE projects SET strategy=%s, updated_at=now() WHERE id=%s",
            (Jsonb(strategy), pid),
        )


def get_project_config(pid: str) -> dict[str, Any]:
    with _conn() as c:
        row = c.execute("SELECT config FROM projects WHERE id=%s", (pid,)).fetchone()
    if row is None:
        raise KeyError(f"project {pid} not found")
    return row[0] or {}


def set_project_config(
    pid: str, config: dict[str, Any], *, connection: Connection[Any] | None = None
) -> None:
    with _conn(connection) as c:
        c.execute(
            "UPDATE projects SET config=%s, updated_at=now() WHERE id=%s", (Jsonb(config), pid)
        )


def set_project_source(
    pid: str,
    source_path: str,
    book_title: Optional[str],
    *,
    source_sha256: str | None = None,
    fmt: str | None = None,
    source_meta: dict[str, Any] | None = None,
) -> None:
    with _conn() as c:
        c.execute(
            """UPDATE projects SET source_path=%s, book_title=%s,
                source_sha256=COALESCE(%s,source_sha256), fmt=COALESCE(%s,fmt),
                source_meta=COALESCE(%s,source_meta), updated_at=now() WHERE id=%s""",
            (
                source_path,
                book_title,
                source_sha256,
                fmt,
                Jsonb(source_meta) if source_meta is not None else None,
                pid,
            ),
        )


def chapter_review_state(
    review: dict[str, Any] | None, meta: dict[str, Any], fallback_status: str = "pending"
) -> tuple[str, bool]:
    """Return current review status and whether AI findings still apply to the text.

    Review history is immutable. A later human edit invalidates its current badges;
    a later human completion or a new whole-book review establishes a fresh status.
    """

    def timestamp(value: Any) -> float:
        if not isinstance(value, str):
            return 0.0
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except ValueError:
            return 0.0

    review = review or {}
    reviewed = timestamp(
        review.get("finished_at") or review.get("interrupted_at") or review.get("started_at")
    )
    edited = timestamp(meta.get("review_invalidated_at"))
    manual = timestamp(meta.get("manual_reviewed_at"))
    if edited and edited >= max(reviewed, manual):
        return "pending", False
    if manual and manual >= max(reviewed, edited):
        return "completed", False
    if review:
        return review.get("status") or "pending", True
    return fallback_status, False


def chapter_summaries(pid: str) -> list[dict]:
    """Return chapter summaries with source/target counts and review status."""
    with _conn() as c:
        rows = c.execute(
            """SELECT ch.seq, ch.title, ch.title_translated, ch.status,
                      (SELECT COUNT(*) FROM segments s
                        WHERE s.project_id=ch.project_id AND s.chapter_seq=ch.seq
                          AND s.source<>'' AND s.kind='text') AS src_words,
                      (SELECT COUNT(*) FROM segments s
                        WHERE s.project_id=ch.project_id AND s.chapter_seq=ch.seq
                          AND s.source<>'' AND s.target IS NOT NULL
                          AND s.kind='text') AS tgt_words,
                      ch.review_status,ch.meta
                 FROM chapters ch WHERE ch.project_id=%s ORDER BY ch.seq""",
            (pid,),
        ).fetchall()
        latest = c.execute(
            """SELECT value FROM artifacts WHERE project_id=%s
                AND key ~ '^reviews/review-[^/]+/result[.]json$'
                ORDER BY key DESC LIMIT 1""",
            (pid,),
        ).fetchone()
    review = latest[0] if latest and isinstance(latest[0], dict) else {}
    counts: dict[int, int] = {}
    for issue in review.get("issues") or []:
        chapter = issue.get("chapter")
        if isinstance(chapter, int):
            counts[chapter] = counts.get(chapter, 0) + 1
    return [
        {
            "index": r[0],
            "title": r[1] or "",
            "title_translated": r[2],
            "status": r[3] or "pending",
            "word_count": r[4] or 0,
            "target_word_count": r[5] or 0,
            "review_issue_count": counts.get(r[0], 0)
            if chapter_review_state(review, r[7] or {}, r[6])[1]
            else 0,
            "review_status": chapter_review_state(review, r[7] or {}, r[6])[0],
        }
        for r in rows
    ]


def set_chapter_status(pid: str, chapter_index: int, status: str) -> None:
    with _conn() as c:
        c.execute(
            """UPDATE chapters SET status=%s
               WHERE project_id=%s AND seq=%s""",
            (status, pid, chapter_index),
        )


def total_word_count(pid: str) -> int:
    with _conn() as c:
        r = c.execute(
            """SELECT COUNT(*) FROM segments
               WHERE project_id=%s AND source<>'' AND kind='text'""",
            (pid,),
        ).fetchone()
    return r[0] if r else 0


def create_job(
    pid: str,
    kind: str,
    arq_job_id: str,
    *,
    params: dict[str, Any] | None = None,
    config_snapshot: dict[str, Any] | None = None,
    run_id: str | None = None,
    status: str = "queued",
) -> int:
    saved_params = dict(params or {})
    if config_snapshot is not None:
        saved_params["config_snapshot"] = config_snapshot
    with _conn() as c:
        r = c.execute(
            """INSERT INTO jobs (project_id, kind, status, arq_job_id, params, run_id)
               VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
            (pid, kind, status, arq_job_id, Jsonb(saved_params), run_id or uuid.uuid4().hex),
        ).fetchone()
    assert r is not None
    return r[0]


def set_job_status(
    job_id: int, status: str, error: Optional[str] = None, *, result: dict[str, Any] | None = None
) -> None:
    with _conn() as c:
        c.execute(
            """UPDATE jobs SET status=%s, error=%s, result=COALESCE(%s,result),
               updated_at=now() WHERE id=%s""",
            (status, error, Jsonb(result) if result is not None else None, job_id),
        )


_JOB_COLUMNS = (
    "id,project_id,kind,status,arq_job_id,run_id,params,result,error,created_at,updated_at"
)


def _job_row(row) -> dict[str, Any] | None:
    if row is None:
        return None
    result: dict[str, Any] = dict(zip(_JOB_COLUMNS.split(","), row))
    for key in ("created_at", "updated_at"):
        result[key] = result[key].isoformat() if result[key] else None
    return result


def get_job(job_id: int) -> dict[str, Any] | None:
    with _conn() as c:
        row = c.execute(f"SELECT {_JOB_COLUMNS} FROM jobs WHERE id=%s", (job_id,)).fetchone()
    return _job_row(row)


def get_job_by_arq_id(arq_job_id: str) -> dict[str, Any] | None:
    with _conn() as c:
        row = c.execute(
            f"SELECT {_JOB_COLUMNS} FROM jobs WHERE arq_job_id=%s ORDER BY id DESC LIMIT 1",
            (arq_job_id,),
        ).fetchone()
    return _job_row(row)


def latest_resumable_job(pid: str) -> dict[str, Any] | None:
    with _conn() as c:
        row = c.execute(
            f"""SELECT {_JOB_COLUMNS} FROM jobs WHERE project_id=%s
                AND kind <> 'export' ORDER BY id DESC LIMIT 1""",
            (pid,),
        ).fetchone()
    result = _job_row(row)
    if result is None or result["status"] not in {"paused", "error", "interrupted"}:
        return None
    return result


def list_jobs(pid: str) -> list[dict[str, Any]]:
    with _conn() as c:
        rows = c.execute(
            f"SELECT {_JOB_COLUMNS} FROM jobs WHERE project_id=%s ORDER BY id DESC", (pid,)
        ).fetchall()
    return [item for item in (_job_row(row) for row in rows) if item is not None]


def job_review_id(job_id: int) -> str | None:
    """Associate a review with its execution using persisted, project-scoped events."""
    with _conn() as conn:
        row = conn.execute(
            """SELECT e.payload->>'review_id' FROM events e JOIN jobs j ON j.id=%s
               WHERE e.project_id=j.project_id AND e.created_at>=j.created_at
                 AND e.type IN ('review_started','review_autofix_finished')
                 AND e.payload->>'review_id' IS NOT NULL
                 AND NOT EXISTS (
                     SELECT 1 FROM jobs next WHERE next.project_id=j.project_id
                       AND next.kind<>'export' AND next.id>j.id
                       AND e.created_at>=next.created_at)
               ORDER BY e.id DESC LIMIT 1""",
            (job_id,),
        ).fetchone()
    return row[0] if row else None


def create_export(pid: str, fmt: str, options: dict[str, Any]) -> int:
    with _conn() as c:
        row = c.execute(
            """INSERT INTO exports (project_id, format, options, status)
               VALUES (%s,%s,%s,'pending') RETURNING id""",
            (pid, fmt, Jsonb(options)),
        ).fetchone()
    assert row is not None
    return row[0]


def set_export_status(
    export_id: int,
    status: str,
    *,
    path: Optional[str] = None,
    size: Optional[int] = None,
    error: str | None = None,
) -> None:
    with _conn() as c:
        c.execute(
            """UPDATE exports SET status=%s, path=%s, size=%s, error=%s WHERE id=%s""",
            (status, path, size, error, export_id),
        )


def is_paused(pid: str) -> bool:
    """Return whether the project is paused or pausing."""
    with _conn() as c:
        r = c.execute("SELECT status FROM projects WHERE id=%s", (pid,)).fetchone()
    return bool(r and r[0] in {"paused", "pausing"})
