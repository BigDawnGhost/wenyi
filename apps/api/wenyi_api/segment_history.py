"""Paragraph revisions live beside chapter state, without adding domain history fields."""

from __future__ import annotations

from typing import Literal

from psycopg import Connection
from wenyi_core.ingest.models import Chapter


def record_chapter_changes(
    conn: Connection, pid: str, chapter: Chapter, *, kind: Literal["manual"] | None = None
) -> None:
    previous = {
        row[0]: (row[1], row[2])
        for row in conn.execute(
            "SELECT seg_seq,target,target_before_polish FROM segments WHERE project_id=%s AND chapter_seq=%s",
            (pid, chapter.index),
        ).fetchall()
    }
    changes = []
    for segment in chapter.segments:
        before, old_polish = previous.get(segment.index, (None, None))
        after, unpolished = segment.target, segment.target_before_polish
        if before == after:
            continue
        base = (pid, chapter.index, segment.index)
        if (
            kind is None
            and unpolished is not None
            and unpolished != old_polish
            and unpolished != after
        ):
            # Translation and polishing can be persisted together at the end of one batch.
            if before != unpolished:
                changes.append(
                    (*base, "translation" if before is None else "update", before, unpolished)
                )
            changes.append((*base, "polish", unpolished, after))
        else:
            changes.append(
                (*base, kind or ("translation" if before is None else "update"), before, after)
            )
    if changes:
        with conn.cursor() as cursor:
            cursor.executemany(
                """INSERT INTO segment_revisions(project_id,chapter_seq,seg_seq,kind,previous_target,new_target)
                   VALUES(%s,%s,%s,%s,%s,%s)""",
                changes,
            )


def load_history(conn: Connection, pid: str, ci: int, si: int) -> list[dict]:
    segment = conn.execute(
        "SELECT target,target_before_polish FROM segments WHERE project_id=%s AND chapter_seq=%s AND seg_seq=%s",
        (pid, ci, si),
    ).fetchone()
    if segment is None:
        raise KeyError("segment not found")
    rows = conn.execute(
        """SELECT 'revision-' || id AS key,kind,previous_target,new_target,created_at,id
           FROM segment_revisions WHERE project_id=%s AND chapter_seq=%s AND seg_seq=%s
           UNION ALL
           SELECT 'event-' || id,'manual',payload->>'before',payload->>'after',created_at,id
           FROM events WHERE project_id=%s AND type='manual_translation_edited'
             AND payload->>'chapter'=%s AND payload->>'index'=%s
             AND COALESCE(payload->>'history_recorded','false') != 'true'
             AND payload ? 'before' AND payload ? 'after'
             AND payload->>'before' IS DISTINCT FROM payload->>'after'
           ORDER BY created_at DESC,id DESC""",
        (pid, ci, si, pid, str(ci), str(si)),
    ).fetchall()
    history = [
        {"id": key, "kind": kind, "before": before, "after": after, "created_at": date.isoformat()}
        for key, kind, before, after, date, _ in rows
    ]
    target, unpolished = segment
    # Existing saved snapshots have no reliable timestamp or intervening automatic changes.
    if target is not None and (not history or history[0]["after"] != target):
        history.insert(
            0,
            {
                "id": "current-snapshot",
                "kind": "snapshot",
                "before": None,
                "after": target,
                "created_at": None,
            },
        )
    if unpolished is not None and not any(
        unpolished in (entry["before"], entry["after"]) for entry in history
    ):
        history.append(
            {
                "id": "before-polish",
                "kind": "before_polish",
                "before": None,
                "after": unpolished,
                "created_at": None,
            }
        )
    return history
