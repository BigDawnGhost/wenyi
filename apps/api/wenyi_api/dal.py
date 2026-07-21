"""项目级数据访问（不属于内核 Storage Protocol，但 API 需要的列表/统计查询）。"""

from __future__ import annotations

import uuid
from typing import Any, Optional

from psycopg.types.json import Json

from .db import get_pool


def _conn():
    return get_pool().connection()


def create_project(name: str, source_lang: str, target_lang: str,
                   strategy: dict[str, Any]) -> str:
    pid = uuid.uuid4().hex[:16]
    with _conn() as c:
        c.execute(
            """INSERT INTO projects (id, name, source_lang, target_lang, status, strategy)
               VALUES (%s,%s,%s,%s,'created',%s)""",
            (pid, name, source_lang, target_lang, Json(strategy)),
        )
    return pid


def list_projects() -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            """SELECT id, name, title, fmt, source_lang, target_lang, status, created_at
               FROM projects ORDER BY created_at DESC"""
        ).fetchall()
    return [
        {"id": r[0], "name": r[1], "title": r[2], "fmt": r[3],
         "source_lang": r[4], "target_lang": r[5], "status": r[6],
         "created_at": r[7].isoformat() if r[7] else None}
        for r in rows
    ]


def get_project(pid: str) -> Optional[dict]:
    with _conn() as c:
        r = c.execute(
            """SELECT id, name, title, fmt, source_lang, target_lang, status,
                      strategy, book_title, created_at
               FROM projects WHERE id=%s""",
            (pid,),
        ).fetchone()
    if r is None:
        return None
    return {
        "id": r[0], "name": r[1], "title": r[2], "fmt": r[3],
        "source_lang": r[4], "target_lang": r[5], "status": r[6],
        "strategy": r[7], "book_title": r[8],
        "created_at": r[9].isoformat() if r[9] else None,
    }


def set_project_status(pid: str, status: str) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE projects SET status=%s, updated_at=now() WHERE id=%s",
            (status, pid),
        )


def set_project_strategy(pid: str, strategy: dict[str, Any]) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE projects SET strategy=%s, updated_at=now() WHERE id=%s",
            (Json(strategy), pid),
        )


def set_project_source(pid: str, source_path: str, book_title: Optional[str]) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE projects SET source_path=%s, book_title=%s WHERE id=%s",
            (source_path, book_title, pid),
        )


def chapter_summaries(pid: str) -> list[dict]:
    """章节列表 + 原文/译文词数 + 审校问题数。"""
    with _conn() as c:
        rows = c.execute(
            """SELECT ch.seq, ch.title, ch.title_translated, ch.status,
                      (SELECT COUNT(*) FROM segments s
                        WHERE s.project_id=ch.project_id AND s.chapter_seq=ch.seq
                          AND s.source<>'' AND s.kind='text') AS src_words,
                      (SELECT COUNT(*) FROM segments s
                        WHERE s.project_id=ch.project_id AND s.chapter_seq=ch.seq
                          AND s.target IS NOT NULL AND s.target<>''
                          AND s.kind='text') AS tgt_words,
                      COALESCE((ch.meta->>'_review_issue_count')::int,
                               jsonb_array_length(
                                  COALESCE(ch.meta->'review_issues','[]'::jsonb)), 0)
                 FROM chapters ch WHERE ch.project_id=%s ORDER BY ch.seq""",
            (pid,),
        ).fetchall()
    return [
        {"index": r[0], "title": r[1] or "", "title_translated": r[2],
         "status": r[3] or "pending", "word_count": r[4] or 0,
         "target_word_count": r[5] or 0, "review_issue_count": r[6] or 0}
        for r in rows
    ]


def total_word_count(pid: str) -> int:
    with _conn() as c:
        r = c.execute(
            """SELECT COUNT(*) FROM segments
               WHERE project_id=%s AND source<>'' AND kind='text'""",
            (pid,),
        ).fetchone()
    return r[0] if r else 0


def create_job(pid: str, kind: str, arq_job_id: str) -> int:
    with _conn() as c:
        r = c.execute(
            """INSERT INTO jobs (project_id, kind, status, arq_job_id)
               VALUES (%s,%s,'running',%s) RETURNING id""",
            (pid, kind, arq_job_id),
        ).fetchone()
    return r[0]


def set_job_status(job_id: int, status: str, error: Optional[str] = None) -> None:
    with _conn() as c:
        c.execute(
            """UPDATE jobs SET status=%s, error=%s, updated_at=now() WHERE id=%s""",
            (status, error, job_id),
        )


def create_export(pid: str, fmt: str, options: dict[str, Any]) -> int:
    with _conn() as c:
        row = c.execute(
            """INSERT INTO exports (project_id, format, options, status)
               VALUES (%s,%s,%s,'pending') RETURNING id""",
            (pid, fmt, Json(options)),
        ).fetchone()
    return row[0]


def set_export_status(export_id: int, status: str, *,
                      path: Optional[str] = None,
                      size: Optional[int] = None) -> None:
    with _conn() as c:
        c.execute(
            """UPDATE exports SET status=%s, path=%s, size=%s WHERE id=%s""",
            (status, path, size, export_id),
        )


def is_paused(pid: str) -> bool:
    """项目当前是否处于暂停态（用 projects.status='paused' 判定）。"""
    with _conn() as c:
        r = c.execute(
            "SELECT status FROM projects WHERE id=%s", (pid,)
        ).fetchone()
    return bool(r and r[0] == "paused")
