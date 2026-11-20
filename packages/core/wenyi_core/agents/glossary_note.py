"""Bounded JSON evidence loop over one term and a frozen window of translated passages."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from pydantic import ValidationError

from ..glossary.notes import PAGE, REF_PATTERN, Fact, NoteJournal
from ..glossary.store import source_matches_text
from ..llm.json_parser import JsonParseError, parse_json_result
from ..models.glossary import GlossaryTerm
from .base import Agent, Messages
from .glossary_extractor import TranslatedSegmentEvidence
from .note_tools import NoteEvidence


class _RejectedFact(ValueError):
    """A model-content decision with an audit-safe reason, never a provider failure."""

    def __init__(self, reason: str, trigger_indices: tuple[int, ...] = ()):
        super().__init__(reason)
        self.reason = reason
        self.trigger_indices = trigger_indices


@dataclass
class NoteResult:
    lines: list[str] = field(default_factory=list)
    rejections: dict[str, str] = field(default_factory=dict)

    @property
    def outcome(self) -> str:
        if self.rejections:
            return "partial" if self.lines else "not_adopted"
        return "accepted" if self.lines else "no_update"


def _object(raw: str) -> dict | None:
    try:
        parsed = parse_json_result(raw)
    except JsonParseError:
        return None
    return (
        parsed.value
        if parsed.safe_for_complete_payload and isinstance(parsed.value, dict)
        else None
    )


@dataclass(frozen=True)
class NoteWindow:
    chapter: int
    start: int  # Chapter-local text_segments ordinal, as in Review.
    pairs: tuple[tuple[str, str], ...]
    history: Mapping[tuple[int, int], TranslatedSegmentEvidence] = field(default_factory=dict)

    def read(self, index: object, offset: object) -> dict:
        if type(index) is not int or not self.start <= index < self.start + len(self.pairs):
            raise ValueError("Glossary context read outside window")
        source, target = self.pairs[index - self.start]
        if type(offset) is not int or not 0 <= offset < len(source):
            raise ValueError("Invalid glossary context offset")
        excerpt = source[offset : offset + PAGE]
        return {
            "ref": f"c{self.chapter}:s{index}:{offset}-{offset + len(excerpt)}",
            "source": excerpt,
            "source_length": len(source),
            "translation_preview": target[:PAGE],
        }

    def reread(self, ref: object, allowed: set[str]) -> dict:
        if (
            not isinstance(ref, str)
            or ref not in allowed
            or not (m := re.fullmatch(REF_PATTERN, ref))
        ):
            raise ValueError("Unknown glossary history reference")
        chapter, index, start, end = map(int, m.groups())
        if chapter == self.chapter and self.start <= index < self.start + len(self.pairs):
            source = self.pairs[index - self.start][0]
        elif (chapter, index) < (self.chapter, self.start) and (
            old := self.history.get((chapter, index))
        ):
            source = old.source
        else:
            raise ValueError("Glossary reference outside translated history")
        if not 0 <= start < end <= len(source) or end - start > PAGE:
            raise ValueError("Invalid glossary history slice")
        return {"ref": ref, "source": source[start:end]}


class GlossaryNoteAgent(Agent):
    def update(self, term: GlossaryTerm, window: NoteWindow, hits: list[int]) -> NoteResult:
        preview = window.read(hits[0], 0)
        journal = NoteJournal(term.note)
        evidence = NoteEvidence(window, journal, preview)
        # Workers starting at the same passage share its exact prefix; the term,
        # journal and occurrence list vary. Do not add other passages just for cache hits.
        packet = {
            "chapter": window.chapter,
            "window": [window.start, window.start + len(window.pairs) - 1],
            "first_evidence": preview,
            "term": term.source,
            "target": term.target,
            "type": term.type,
            "active_facts": journal.page("guidance", 0),
            "history_entries": len(journal.history),
            "has_editor_note": bool(journal.editor),
            "occurrences": hits[:8],
            "last_occurrence": hits[-1],
        }
        messages: Messages = [
            {"role": "system", "content": self.render("glossary_note_system")},
            {"role": "user", "content": json.dumps(packet, ensure_ascii=False)},
        ]
        reads_used = 0
        repair_available = True
        while True:
            raw = self.client.complete(messages, operation="glossary.note", json_mode=True)
            data = _object(raw) or {}
            updates = data.get("updates")
            if set(data) == {"updates"} and isinstance(updates, list) and len(updates) <= 2:
                return self._facts(messages, raw, updates, evidence, journal, repair_available)
            reads = data.get("reads")
            reason = "invalid_note_response"
            if (
                set(data) == {"reads"}
                and isinstance(reads, list)
                and 1 <= len(reads) <= 2
                and reads_used < 2
                and repair_available
            ):
                try:
                    results = []
                    for item in reads:
                        if not isinstance(item, dict):
                            raise ValueError("invalid_read")
                        results.append(
                            evidence.tools.call(
                                item.get("tool"), {k: v for k, v in item.items() if k != "tool"}
                            )
                        )
                except ValueError:
                    # A partially executed group must not authorize unseen evidence.
                    return NoteResult(rejections={"response": "invalid_evidence_request"})
                messages.extend(
                    [
                        {"role": "assistant", "content": raw},
                        {
                            "role": "user",
                            "content": json.dumps({"results": results}, ensure_ascii=False),
                        },
                    ]
                )
                reads_used += 1
                continue
            if not repair_available:
                return NoteResult(rejections={"response": reason})
            repair_available = False
            messages.extend(
                [
                    {"role": "assistant", "content": raw},
                    {
                        "role": "user",
                        "content": 'Invalid note response. Return one JSON object with only "updates": an array of at most two supported facts, or []. No tool calls. Retain all local conditions, or withdraw the unsupported fact.',
                    },
                ]
            )

    def _facts(
        self,
        messages: Messages,
        raw: str,
        updates: list,
        evidence: NoteEvidence,
        journal: NoteJournal,
        repair_available: bool,
    ) -> NoteResult:
        """Only repair ungrounded triggers; never ask the model to rewrite a valid sibling."""
        result = NoteResult()
        accepted: list[dict] = []
        pending: dict[int, dict] = {}
        slots: list[tuple[int, int]] = []

        def accept(i: int, item: dict) -> tuple[int, ...]:
            try:
                lines = self._validated_lines(
                    [*accepted, item], evidence.cited, evidence.fresh, journal
                )
            except ValidationError:
                result.rejections[str(i)] = "invalid_fact_schema"
            except _RejectedFact as error:
                result.rejections[str(i)] = error.reason
                return error.trigger_indices
            else:
                accepted.append(item)
                result.lines = lines
                result.rejections.pop(str(i), None)
            return ()

        for i, item in enumerate(updates):
            missing = accept(i, item)
            if missing:
                pending[i] = item
                slots.extend((i, j) for j in missing)
        if not slots or not repair_available:
            return result
        paths = ", ".join(f"updates[{i}].when[{j}]" for i, j in slots)
        feedback = (
            f"Fact triggers must occur in the cited source: {paths}. For these slots in order, "
            'return only {"replacements":[...]}. Copy the intended spelling from the cited source; '
            "use null to withdraw the entire unsupported fact. Never broaden a condition. No tool calls."
        )
        # Do not catch transport, quota, or cancellation errors as editorial rejections.
        reply = self.client.complete(
            [
                *messages,
                {"role": "assistant", "content": raw},
                {"role": "user", "content": feedback},
            ],
            operation="glossary.note",
            json_mode=True,
        )
        data = _object(reply) or {}
        values = data.get("replacements")
        if (
            set(data) != {"replacements"}
            or not isinstance(values, list)
            or len(values) != len(slots)
        ):
            result.rejections.update({str(i): "invalid_trigger_repair" for i in pending})
            return result
        for (i, j), value in zip(slots, values):
            if i not in pending:
                continue
            if not isinstance(value, str) or not value.strip():
                result.rejections[str(i)] = (
                    "withdrawn" if value is None else "invalid_trigger_repair"
                )
                del pending[i]
                continue
            pending[i]["when"][j] = value
        for i, item in pending.items():
            accept(i, item)
        return result

    @staticmethod
    def _validated_lines(
        updates: object, cited: dict[str, str], fresh: set[str], journal: NoteJournal
    ) -> list[str]:
        if not isinstance(updates, list) or len(updates) > 2:
            raise ValueError("Invalid glossary note updates")
        seen = {f.text.casefold() for f in journal.active.values()}
        corrected: set[str] = set()
        lines = []
        for item in updates:
            fact = Fact.model_validate(item)
            refs = set(fact.refs)
            if not refs <= cited.keys() or not refs & fresh:
                raise _RejectedFact("unseen_evidence")
            missing = tuple(
                i
                for i, w in enumerate(fact.when)
                if not any(source_matches_text(w, cited[r]) for r in refs)
            )
            if missing:
                raise _RejectedFact("unmatched_trigger", missing)
            if fact.corrects:
                old = journal.active.get(fact.corrects)
                if old is None or fact.corrects in corrected or not (refs & fresh) - set(old.refs):
                    raise _RejectedFact("unsupported_correction")
                corrected.add(fact.corrects)
            if fact.text.casefold() in seen:
                if fact.corrects:
                    raise _RejectedFact("duplicate_correction")
                continue
            seen.add(fact.text.casefold())
            lines.append(fact.line())
        return lines
