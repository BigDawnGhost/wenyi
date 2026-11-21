"""Bind glossary tools to the exact evidence available in one model conversation."""

from __future__ import annotations

from typing import Protocol

from ..agent_tools import BODY_TOOLS, NOTE_TOOLS, BoundTools, ToolError
from ..glossary.notes import NoteJournal, note_page
from ..glossary.store import GlossaryStore
from ..models.glossary import GlossaryTerm


class WindowReads(Protocol):
    def read(self, index: object, offset: object) -> dict: ...
    def reread(self, ref: object, allowed: set[str]) -> dict: ...


class NoteEvidence:
    """Only successful source reads, not note pages, can authorize a citation."""

    def __init__(self, window: WindowReads, journal: NoteJournal, preview: dict):
        self._window = window
        self._journal = journal
        self.fresh = {preview["ref"]}
        self.cited = {preview["ref"]: preview["source"]}
        self.tools = BoundTools(
            NOTE_TOOLS,
            read_note=lambda args: journal.page(args["section"], args.get("offset", 0)),
            read_reference=self._read_reference,
            read_context=self._read_context,
        )

    def _read_reference(self, args: dict) -> dict:
        passage = self._window.reread(args["ref"], self._journal.refs)
        self.cited[passage["ref"]] = passage["source"]
        return passage

    def _read_context(self, args: dict) -> dict:
        passage = self._window.read(args["index"], args.get("offset", 0))
        self.cited[passage["ref"]] = passage["source"]
        self.fresh.add(passage["ref"])
        return passage


def batch_note_tools(terms: list[GlossaryTerm], source: str) -> BoundTools | None:
    matched = {t.source: t.note for t in GlossaryStore.terms_in(terms, source) if t.note.strip()}
    if not matched:
        return None

    def read_note(args: dict) -> dict:
        term = args["term"]
        if not isinstance(term, str) or term not in matched:
            raise ToolError("Cannot read a glossary note outside this batch")
        return note_page(matched[term], args["section"], args.get("offset", 0))

    return BoundTools(BODY_TOOLS, read_note=read_note)
