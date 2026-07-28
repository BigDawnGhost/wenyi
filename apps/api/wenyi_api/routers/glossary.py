"""术语表：列表 / 搜索 / 增删改 / 锁定 / 冲突解决。"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from wenyi_core.glossary.store import GlossaryTerm

from ..db import get_pool
from ..schemas import ConflictOut, Message, ResolveConflict, TermIn, TermOut
from ..storage_pg import PostgresStorage

router = APIRouter(prefix="/projects/{pid}/glossary", tags=["glossary"])


def _storage(pid: str) -> PostgresStorage:
    return PostgresStorage(pid, get_pool())


@router.get("/terms", response_model=list[TermOut])
def list_terms(pid: str, q: Optional[str] = Query(None),
               type: Optional[str] = Query(None),
               locked: Optional[bool] = Query(None)) -> list[dict]:
    storage = _storage(pid)
    terms = storage.all_terms()
    if type:
        terms = [t for t in terms if t.type == type]
    if locked is not None:
        terms = [t for t in terms if bool(t.locked) == locked]
    if q:
        ql = q.lower()
        terms = [
            t for t in terms
            if ql in t.source.lower() or ql in t.target.lower()
            or any(ql in (a or "").lower() for a in t.aliases)
        ]
    return [vars(t) for t in terms]


@router.post("/terms", response_model=TermOut, status_code=201)
def add_term(pid: str, body: TermIn) -> dict:
    storage = _storage(pid)
    t = GlossaryTerm(source=body.source, target=body.target, reading=body.reading,
                     type=body.type, gender=body.gender, aliases=body.aliases,
                     note=body.note, confidence=body.confidence, locked=True,
                     status="ok")
    storage.upsert_term(t, chapter=None)
    storage.lock_term(body.source)
    return vars(storage.get_term(body.source))  # type: ignore[arg-type]


@router.put("/terms/{source}", response_model=TermOut)
def update_term(pid: str, source: str, body: TermIn) -> dict:
    storage = _storage(pid)
    existing = storage.get_term(source)
    if existing is None:
        raise HTTPException(404, "term not found")
    locked = existing.locked
    storage.delete_term(source)
    t = GlossaryTerm(source=body.source, target=body.target, reading=body.reading,
                     type=body.type, gender=body.gender, aliases=body.aliases,
                     note=body.note, confidence=body.confidence, locked=locked)
    storage.upsert_term(t)
    return vars(storage.get_term(body.source))  # type: ignore[arg-type]


@router.delete("/terms/{source}", response_model=Message)
def delete_term(pid: str, source: str) -> dict:
    storage = _storage(pid)
    ok = storage.delete_term(source)
    return {"message": "deleted" if ok else "not found"}


@router.post("/terms/{source}/lock", response_model=Message)
def lock_term(pid: str, source: str, target: Optional[str] = None) -> dict:
    _storage(pid).lock_term(source, target)
    return {"message": "locked"}


@router.post("/terms/{source}/unlock", response_model=Message)
def unlock_term(pid: str, source: str) -> dict:
    with get_pool().connection() as c:
        c.execute(
            "UPDATE glossary SET locked=FALSE WHERE project_id=%s AND source=%s",
            (pid, source),
        )
    return {"message": "unlocked"}


@router.get("/conflicts", response_model=list[ConflictOut])
def list_conflicts(pid: str) -> list[dict]:
    return _storage(pid).open_conflicts()


@router.post("/conflicts/{cid}/resolve", response_model=Message)
def resolve_conflict(pid: str, cid: int, body: ResolveConflict) -> dict:
    storage = _storage(pid)
    with get_pool().connection() as c:
        row = c.execute(
            """SELECT source, existing_target, proposed_target
               FROM term_conflicts WHERE id=%s AND project_id=%s""",
            (cid, pid),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "conflict not found")
    source, existing, proposed = row[0], row[1], row[2]
    if body.decision == "current":
        target = existing
    elif body.decision == "proposed":
        target = proposed
    else:
        target = body.target or existing
    if target is not None:
        storage.lock_term(source, target)
    with get_pool().connection() as c:
        c.execute(
            "UPDATE term_conflicts SET resolved=TRUE WHERE id=%s", (cid,)
        )
        c.execute(
            """UPDATE glossary SET status='ok' WHERE project_id=%s AND source=%s""",
            (pid, source),
        )
    return {"message": "resolved"}


@router.get("/export")
def export_glossary(pid: str, format: str = "json"):
    """导出术语表（JSON / CSV）。"""
    terms = _storage(pid).all_terms()
    if format == "csv":
        import csv
        import io

        from fastapi.responses import StreamingResponse
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["source", "target", "reading", "type", "gender",
                    "aliases", "confidence", "locked"])
        for t in terms:
            w.writerow([t.source, t.target, t.reading, t.type, t.gender,
                        "|".join(t.aliases), t.confidence, int(t.locked)])
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=glossary-{pid}.csv"},
        )
    rows = [vars(t) for t in terms]
    from fastapi.responses import JSONResponse
    return JSONResponse(rows, headers={"Content-Disposition": f"attachment; filename=glossary-{pid}.json"})


@router.post("/import")
def import_glossary(pid: str, body: dict) -> dict:
    """批量导入术语（JSON 数组）。"""
    storage = _storage(pid)
    items = body.get("terms") if isinstance(body, dict) else body
    count = 0
    for it in items or []:
        t = GlossaryTerm(
            source=it["source"], target=it["target"],
            reading=it.get("reading", ""), type=it.get("type", "术语"),
            gender=it.get("gender", ""), aliases=it.get("aliases", []),
            note=it.get("note", ""), confidence=it.get("confidence", "medium"),
            locked=bool(it.get("locked", False)))
        storage.upsert_term(t)
        count += 1
    return {"imported": count}
