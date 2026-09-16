"""Durable interactive requests, isolated from the long-running workflow job."""

from __future__ import annotations

from contextlib import nullcontext
from uuid import uuid4

from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .db import get_pool
from .project_service import (
    config_document,
    effective_config,
    require_book,
    require_project,
    storage_for,
)


def requests_for(pid: str) -> list[dict]:
    with get_pool().connection() as conn:
        rows = (
            conn.cursor(row_factory=dict_row)
            .execute(
                "SELECT id,chapter_index,segment_indices,status,applied,conflicts,error,created_at "
                "FROM retranslation_requests WHERE project_id=%s ORDER BY created_at DESC",
                (pid,),
            )
            .fetchall()
        )
    for row in rows:
        row["created_at"] = row["created_at"].isoformat()
    return rows


def get_request(pid: str, request_id: str) -> dict | None:
    with get_pool().connection() as conn:
        return (
            conn.cursor(row_factory=dict_row)
            .execute(
                "SELECT * FROM retranslation_requests WHERE project_id=%s AND id=%s",
                (pid, request_id),
            )
            .fetchone()
        )


def set_result(
    pid,
    request_id,
    status,
    *,
    applied=None,
    conflicts=None,
    error=None,
    usage=None,
    elapsed_seconds=None,
    connection=None,
):
    with nullcontext(connection) if connection is not None else get_pool().connection() as conn:
        conn.execute(
            """UPDATE retranslation_requests SET status=%s,applied=COALESCE(%s,applied),
            conflicts=COALESCE(%s,conflicts),error=%s,usage=COALESCE(%s,usage),
            elapsed_seconds=COALESCE(%s,elapsed_seconds),updated_at=now()
            WHERE project_id=%s AND id=%s""",
            (
                status,
                Jsonb(applied) if applied is not None else None,
                Jsonb(conflicts) if conflicts is not None else None,
                error,
                Jsonb(usage) if usage is not None else None,
                elapsed_seconds,
                pid,
                request_id,
            ),
        )


def create_request(pid: str, ci: int, indices: list[int]) -> dict:
    storage = storage_for(pid)
    try:
        with storage.state_lock():
            project = require_project(pid)
            require_book(project)
            if not project.get("initialized"):
                raise HTTPException(409, "请等待原文初始化完成后再重译段落")
            try:
                chapter = storage.load_chapter(ci)
            except KeyError:
                raise HTTPException(404, "chapter not found") from None
            by_index = {s.index: s for s in chapter.segments}
            for index in indices:
                segment = by_index.get(index)
                if segment is None or not segment.source.strip():
                    raise HTTPException(404, "所选段落不存在或没有可翻译的原文")
                if segment.target is None:
                    raise HTTPException(409, "请先等待所选段落完成首次翻译")
            active = [r for r in requests_for(pid) if r["status"] in {"queued", "running"}]
            if len(active) >= 5:
                raise HTTPException(409, "已有 5 个段落重译任务，请等待完成后再提交")
            if any(
                r["chapter_index"] == ci and set(r["segment_indices"]) & set(indices)
                for r in active
            ):
                raise HTTPException(409, "所选段落已有重译任务，请等待完成后再提交")
            inputs = [
                {
                    "index": s.index,
                    "source": s.source,
                    "before": s.target,
                    "expected_revision": s.meta.get("_wenyi_revision", 0),
                }
                for s in chapter.segments
                if s.index in indices
            ]
            snapshot = config_document(effective_config(project))
            request_id = uuid4().hex
            # Use the state's transaction so deletion and request admission are serialized.
            with storage._conn as conn:
                conn.execute(
                    """INSERT INTO retranslation_requests
                    (id,project_id,chapter_index,segment_indices,inputs,config_snapshot)
                    VALUES(%s,%s,%s,%s,%s,%s)""",
                    (
                        request_id,
                        pid,
                        ci,
                        Jsonb([s["index"] for s in inputs]),
                        Jsonb(inputs),
                        Jsonb(snapshot),
                    ),
                )
            return {"id": request_id}
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    finally:
        storage.close()
