"""Append-only term facts and the active conclusions derived from their correction links."""

from __future__ import annotations

import hashlib
import re
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from .store import source_matches_text

PAGE = 800
REF_PATTERN = r"^c([0-9]{1,10}):s([0-9]{1,10}):([0-9]{1,10})-([0-9]{1,10})$"
_LEGACY = re.compile(r"^\[(guidance|rationale) (c\d+:s\d+:\d+-\d+)\] (.+)$")
_PREFIX = "[fact] "
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=320)]
Ref = Annotated[str, StringConstraints(pattern=REF_PATTERN)]


class Fact(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    text: Text
    refs: list[Ref] = Field(min_length=1, max_length=4)
    when: list[
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=96)]
    ] = Field(default_factory=list, max_length=4)
    corrects: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{16}$")] | None = None
    reason: Text | None = None

    @model_validator(mode="after")
    def validate_event(self) -> Fact:
        if (self.corrects is None) != (self.reason is None):
            raise ValueError("A correction requires both a previous fact ID and a reason")
        if any(len(v.splitlines()) != 1 for v in (self.text, self.reason or "-")):
            raise ValueError("Facts and reasons must each fit on one line")
        if len(set(self.refs)) != len(self.refs):
            raise ValueError("Duplicate evidence references")
        return self

    def line(self) -> str:
        return _PREFIX + self.model_dump_json(exclude_defaults=True)


class NoteJournal:
    def __init__(self, note: str):
        self.active: dict[str, Fact] = {}
        self.editor: list[str] = []
        self.history: list[str] = []
        self.rationale: list[str] = []
        self.refs: set[str] = set()
        self._display: dict[str, str] = {}
        legacy_id = None
        for line in note.splitlines():
            key = hashlib.sha256(line.encode()).hexdigest()[:16]
            if line.startswith(_PREFIX):
                try:
                    fact = Fact.model_validate_json(line[len(_PREFIX) :])
                except ValidationError:
                    # A free-text editor note may legitimately use the same prefix.
                    self.editor.append(line)
                    continue
                if fact.corrects:
                    self.active.pop(fact.corrects, None)
                if fact.reason:
                    self.rationale.append(f"{key} {line}")
                display = fact.text
            elif match := _LEGACY.fullmatch(line):
                self.refs.add(match[2])
                if match[1] == "rationale":
                    self.rationale.append(line)
                    self.history.append(line)
                    continue
                # V1 guidance was a whole-term summary, not a list of independent facts.
                if legacy_id is not None:
                    self.active.pop(legacy_id, None)
                legacy_id = key
                # Legacy/manual lines can be longer than the new writer's output budget.
                fact = Fact.model_construct(text=match[3], refs=[match[2]])
                display = line
            else:
                self.editor.append(line)
                continue
            self.refs.update(fact.refs)
            self.active[key] = fact
            self._display[key] = display
            self.history.append(f"{key} {line}")

    def conclusions(self, source: str | None) -> list[str]:
        return [
            self._display[k]
            for k, fact in self.active.items()
            if source is None
            or not fact.when
            or any(source_matches_text(w, source) for w in fact.when)
        ]

    def page(self, section: object, offset: object) -> dict:
        if not isinstance(section, str) or type(offset) is not int or offset < 0:
            raise ValueError("Invalid glossary note read")
        if section == "guidance":
            lines = [f"{key} {fact.line()}" for key, fact in reversed(self.active.items())]
        elif section == "history":
            lines = list(reversed(self.history))
        elif section == "rationale":
            lines = list(reversed(self.rationale))
        elif section == "editor":
            lines = self.editor
        else:
            raise ValueError("Invalid glossary note section")
        text = "\n".join(lines)
        return {"section": section, "text": text[offset : offset + PAGE], "length": len(text)}


def guidance(note: str, source: str | None = None) -> str:
    log = NoteJournal(note)
    return "\n".join([*log.editor, *log.conclusions(source)]).strip()


def note_page(note: str, section: object, offset: object) -> dict:
    return NoteJournal(note).page(section, offset)
