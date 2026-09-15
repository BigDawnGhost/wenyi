"""Glossary editing, source matching and explicit conflict resolution."""

from __future__ import annotations

import csv
import io
import time
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from psycopg.types.json import Jsonb
from wenyi_core.glossary.store import GlossaryTerm
from wenyi_core.i18n.metadata import normalize_term_type

from ..db import get_pool
from ..project_service import project_write, require_book, require_project, storage_for
from ..schemas import ConflictOut, GlossaryImport, Message, ResolveConflict, TermIn, TermOut

router = APIRouter(prefix="/projects/{pid}/glossary", tags=["glossary"])


def _term(body: TermIn) -> GlossaryTerm:
    if not body.source.strip() or not body.target.strip():
        raise HTTPException(422, "Term source and target must not be empty")
    return GlossaryTerm(**body.model_dump())


@router.get("/terms", response_model=list[TermOut])
def list_terms(pid: str, q: str | None = Query(None), type: str | None = Query(None)) -> list[dict]:
    require_book(require_project(pid))
    terms = storage_for(pid).all_terms()
    if type:
        term_type = normalize_term_type(type)
        terms = [term for term in terms if term.type == term_type]
    if q:
        needle = q.casefold()
        terms = [
            term
            for term in terms
            if any(
                needle in value.casefold() for value in [term.source, term.target, *term.aliases]
            )
        ]
    return [vars(term) for term in terms]


@router.post("/terms", response_model=TermOut, status_code=201)
def add_term(pid: str, body: TermIn) -> dict:
    term = _term(body)
    with project_write(pid) as (project, storage):
        require_book(project)
        if storage.get_term(term.source) is not None:
            raise HTTPException(409, "Term already exists; edit it or resolve its conflicts")
        storage.upsert_term(term)
        storage.log_event("glossary_term_added", source=term.source)
        return vars(storage.get_term(term.source))


@router.put("/terms/{source}", response_model=TermOut)
def update_term(pid: str, source: str, body: TermIn) -> dict:
    term = _term(body)
    with project_write(pid) as (project, storage):
        require_book(project)
        existing = storage.get_term(source)
        if existing is None:
            raise HTTPException(404, "term not found")
        if term.source != source and storage.get_term(term.source) is not None:
            raise HTTPException(409, "Another term already uses that source")
        # Update the row in place: deleting/reinserting would shift prompt glossary order.
        with get_pool().connection() as conn:
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
        storage.log_event("glossary_term_edited", source=term.source, previous_source=source)
        return vars(storage.get_term(term.source))


@router.delete("/terms/{source}", response_model=Message)
def delete_term(pid: str, source: str) -> dict:
    with project_write(pid) as (project, storage):
        require_book(project)
        if not storage.delete_term(source):
            raise HTTPException(404, "term not found")
        storage.mark_conflicts_resolved(source)
        storage.log_event("glossary_term_deleted", source=source)
    return {"message": "deleted"}


@router.get("/conflicts", response_model=list[ConflictOut])
def list_conflicts(pid: str) -> list[dict]:
    require_book(require_project(pid))
    return storage_for(pid).open_conflicts()


@router.post("/conflicts/{cid}/resolve", response_model=Message)
def resolve_conflict(pid: str, cid: int, body: ResolveConflict) -> dict:
    with project_write(pid) as (project, storage):
        require_book(project)
        conflict = next((row for row in storage.open_conflicts() if row["id"] == cid), None)
        if conflict is None:
            raise HTTPException(404, "conflict not found")
        existing = storage.get_term(conflict["source"])
        if existing is None:
            raise HTTPException(404, "term not found")
        if body.decision == "current":
            target = existing.target
        elif body.decision == "proposed":
            target = conflict["proposed_target"]
        else:
            target = body.target
        if not isinstance(target, str) or not target.strip():
            raise HTTPException(422, "A nonempty target is required to resolve the conflict")
        with storage.state_lock():
            storage.resolve_term(existing.source, target)
            storage.mark_conflicts_resolved(existing.source)
            storage.log_event("glossary_conflict_resolved", source=existing.source, target=target)
    return {"message": "resolved"}


@router.get("/export")
def export_glossary(pid: str, format: Literal["json", "csv"] = "json"):
    require_book(require_project(pid))
    terms = storage_for(pid).all_terms()
    if format == "csv":
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            ["source", "target", "reading", "type", "gender", "aliases", "note", "status"]
        )
        for term in terms:
            writer.writerow(
                [
                    term.source,
                    term.target,
                    term.reading,
                    term.type,
                    term.gender,
                    "|".join(term.aliases),
                    term.note,
                    term.status,
                ]
            )
        return Response(
            buffer.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=glossary-{pid}.csv"},
        )
    return JSONResponse(
        [vars(term) for term in terms],
        headers={"Content-Disposition": f"attachment; filename=glossary-{pid}.json"},
    )


@router.post("/import")
def import_glossary(pid: str, body: GlossaryImport) -> dict:
    terms = [_term(item) for item in body.terms]
    with project_write(pid) as (project, storage):
        require_book(project)
        conflicts = 0
        with storage.state_lock():
            for term in terms:
                conflicts += storage.upsert_term(term) == "conflict"
            storage.log_event("glossary_imported", count=len(terms), conflicts=conflicts)
    return {"imported": len(terms), "conflicts": conflicts}
