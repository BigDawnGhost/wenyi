"""Glossary editing, source matching and explicit conflict resolution."""

from __future__ import annotations

import csv
import io
from collections.abc import Awaitable, Callable
from contextlib import closing

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from starlette.types import Message as ASGIMessage
from wenyi_core.glossary.anki import export_anki_glossary
from wenyi_core.i18n.metadata import normalize_term_type
from wenyi_core.models.glossary import (
    STATUS_CONTEXT_DEPENDENT,
    STATUS_OK,
    GlossaryTerm,
    merge_context_senses,
)

from ..context import current_context
from ..errors import GLOSSARY_IMPORT_TOO_LARGE, ApiError
from ..schemas import (
    ConflictOut,
    GlossaryExportFormat,
    GlossaryImport,
    Message,
    ResolveConflict,
    TermIn,
    TermLookup,
    TermOut,
    TermPage,
)
from .dependencies import project_write, read_storage_for, require_project, storage_for

router = APIRouter(prefix="/projects/{pid}/glossary", tags=["glossary"])

MAX_GLOSSARY_IMPORT_MIB = 5
MAX_GLOSSARY_IMPORT_BYTES = MAX_GLOSSARY_IMPORT_MIB * 1024 * 1024


class GlossaryImportRoute(APIRoute):
    """Bound the actual request body before FastAPI buffers it or takes a write lock."""

    def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
        handler = super().get_route_handler()

        async def handle(request: Request) -> Response:
            size = 0

            async def receive() -> ASGIMessage:
                nonlocal size
                message = await request.receive()
                size += len(message.get("body", b""))
                if size > MAX_GLOSSARY_IMPORT_BYTES:
                    raise ApiError(
                        GLOSSARY_IMPORT_TOO_LARGE,
                        413,
                        f"Glossary import request exceeds {MAX_GLOSSARY_IMPORT_MIB} MiB",
                    )
                return message

            return await handler(Request(request.scope, receive))

        return handle


def _term(body: TermIn) -> GlossaryTerm:
    if not body.source.strip() or not body.target.strip():
        raise HTTPException(422, "Term source and target must not be empty")
    return GlossaryTerm(**body.model_dump())


@router.get("/terms", response_model=list[TermOut])
def list_terms(pid: str, q: str | None = Query(None), type: str | None = Query(None)) -> list[dict]:
    require_project(pid)
    with closing(storage_for(pid)) as storage:
        terms = storage.all_terms()
    if type:
        term_type = normalize_term_type(type)
        terms = [term for term in terms if term.type == term_type]
    if q:
        needle = q.strip().casefold()
        terms = [
            term
            for term in terms
            if any(
                needle in value.casefold()
                for value in [term.source, term.target, term.reading, term.note, *term.aliases]
            )
        ]
    return [vars(term) for term in terms]


@router.get("/terms/page", response_model=TermPage)
def list_terms_page(
    pid: str,
    q: str = "",
    type: str = "",
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=200),
) -> dict:
    """Browse by source codepoint order without changing model-context order."""
    require_project(pid)
    storage = read_storage_for(pid)
    try:
        items, total = storage.list_terms_page(
            q=q, term_type=normalize_term_type(type) if type else "", offset=offset, limit=limit
        )
        return {"items": [vars(term) for term in items], "total": total}
    finally:
        storage.close()


@router.post("/terms/lookup", response_model=list[TermOut])
def lookup_terms(pid: str, body: TermLookup) -> list[dict]:
    """Check imported sources against the whole glossary, not just the displayed page."""
    require_project(pid)
    storage = read_storage_for(pid)
    try:
        return [vars(term) for term in storage.get_terms(body.sources)]
    finally:
        storage.close()


@router.post("/terms", response_model=TermOut, status_code=201)
def add_term(pid: str, body: TermIn) -> dict:
    term = _term(body)
    with project_write(pid) as (_project, storage):
        if storage.get_term(term.source) is not None:
            raise HTTPException(409, "Term already exists; edit it or resolve its conflicts")
        storage.upsert_term(term)
        storage.log_event("glossary_term_added", source=term.source)
        return vars(storage.get_term(term.source))


@router.put("/terms/{source}", response_model=TermOut)
def update_term(pid: str, source: str, body: TermIn) -> dict:
    term = _term(body)
    with project_write(pid) as (_project, storage):
        existing = storage.get_term(source)
        if existing is None:
            raise HTTPException(404, "term not found")
        if term.source != source and storage.get_term(term.source) is not None:
            raise HTTPException(409, "Another term already uses that source")
        current_context().update_term(pid, storage, source, term)
        storage.log_event("glossary_term_edited", source=term.source, previous_source=source)
        return vars(storage.get_term(term.source))


@router.delete("/terms/{source}", response_model=Message)
def delete_term(pid: str, source: str) -> dict:
    with project_write(pid) as (_project, storage):
        if not storage.delete_term(source):
            raise HTTPException(404, "term not found")
        storage.mark_conflicts_resolved(source)
        storage.log_event("glossary_term_deleted", source=source)
    return {"message": "deleted"}


@router.get("/conflicts", response_model=list[ConflictOut])
def list_conflicts(pid: str) -> list[dict]:
    require_project(pid)
    with closing(storage_for(pid)) as storage:
        conflicts = storage.open_conflicts()
    counts: dict[str, int] = {}
    for row in conflicts:
        source = row["source"]
        counts[source] = counts.get(source, 0) + 1
    enriched = [{**row, "conflict_count": counts.get(row["source"], 1)} for row in conflicts]
    enriched.sort(key=lambda row: (-row["conflict_count"], row.get("id", 0)))
    return enriched


@router.post("/conflicts/{cid}/resolve", response_model=Message)
def resolve_conflict(pid: str, cid: int, body: ResolveConflict) -> dict:
    with project_write(pid) as (_project, storage):
        open_conflicts = storage.open_conflicts()
        conflict = next((row for row in open_conflicts if row["id"] == cid), None)
        if conflict is None:
            raise HTTPException(404, "conflict not found")
        existing = storage.get_term(conflict["source"])
        if existing is None:
            raise HTTPException(404, "term not found")
        status = STATUS_OK
        if body.decision == "current":
            target = existing.target
        elif body.decision == "proposed":
            target = conflict["proposed_target"]
        elif body.decision == "context_dependent":
            status = STATUS_CONTEXT_DEPENDENT
            if isinstance(body.target, str) and body.target.strip():
                target = merge_context_senses(body.target)
            else:
                candidates = [existing.target]
                for row in open_conflicts:
                    if row["source"] == existing.source and row.get("proposed_target"):
                        candidates.append(row["proposed_target"])
                target = merge_context_senses(*candidates)
        else:
            target = body.target
        if not isinstance(target, str) or not target.strip():
            raise HTTPException(422, "A nonempty target is required to resolve the conflict")
        with storage.state_lock():
            if status == STATUS_CONTEXT_DEPENDENT:
                storage.resolve_term(existing.source, target, status=status)
            else:
                storage.resolve_term(existing.source, target)
            storage.mark_conflicts_resolved(existing.source)
            storage.log_event(
                "glossary_conflict_resolved",
                source=existing.source,
                target=target,
                status=status,
            )
    return {"message": "resolved"}


@router.get("/export")
def export_glossary(pid: str, format: GlossaryExportFormat = "json"):
    project = require_project(pid)
    with closing(storage_for(pid)) as storage:
        terms = storage.all_terms()
    if format == "apkg":
        return Response(
            export_anki_glossary(
                terms,
                project_id=pid,
                title=project.get("book_title") or project["name"],
                source_lang=project.get("source_lang") or "auto",
                target_lang=project.get("target_lang") or "zh",
            ),
            media_type="application/octet-stream",
            headers={"Content-Disposition": f"attachment; filename=glossary-{pid}.apkg"},
        )
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


def import_glossary(pid: str, body: GlossaryImport) -> dict:
    terms = [_term(item) for item in body.terms]
    with project_write(pid) as (_project, storage):
        conflicts = 0
        with storage.state_lock():
            for term in terms:
                conflicts += storage.upsert_term(term) == "conflict"
            storage.log_event("glossary_imported", count=len(terms), conflicts=conflicts)
    return {"imported": len(terms), "conflicts": conflicts}


router.add_api_route(
    "/import",
    import_glossary,
    methods=["POST"],
    responses={
        413: {
            "description": f"Glossary import request exceeds {MAX_GLOSSARY_IMPORT_MIB} MiB",
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["code", "message"],
                        "properties": {
                            "code": {"type": "string", "const": GLOSSARY_IMPORT_TOO_LARGE},
                            "message": {"type": "string"},
                            "request_id": {"type": "string"},
                        },
                    },
                },
            },
        },
    },
    route_class_override=GlossaryImportRoute,
)
