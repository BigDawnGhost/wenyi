"""Read-only model tool contracts. Bind handlers to a snapshot, never to a global book."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

Handler = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class Tool:
    name: str
    required: tuple[str, ...]
    optional: tuple[str, ...]
    description: str

    def usage(self) -> str:
        args = (*self.required, *(f"{arg}?" for arg in self.optional))
        return f"{self.name}({', '.join(args)}): {self.description}"


_PAGE_DESCRIPTION = (
    "Read editor, guidance (active facts), rationale or history; offset is in characters."
)
NOTE_TOOLS = (
    Tool("read_note", ("section",), ("offset",), _PAGE_DESCRIPTION),
    Tool(
        "read_context",
        ("index",),
        ("offset",),
        "Read a source slice and translation preview in the supplied window.",
    ),
    Tool("read_reference", ("ref",), (), "Reopen an exact source slice cited in this term's log."),
)
BODY_TOOLS = (
    Tool(
        "read_note",
        ("term", "section"),
        ("offset",),
        "Use an exact matched source term. " + _PAGE_DESCRIPTION,
    ),
)
REVIEW_TOOLS = (
    Tool(
        "glossary_term",
        ("term",),
        ("section", "offset"),
        "Read one glossary entry by source or alias. " + _PAGE_DESCRIPTION,
    ),
    Tool(
        "term_occurrences",
        ("term",),
        ("selectors", "context_radius"),
        'Select up to 8 occurrences by one-based ordinal or "first", "middle", "last"; context_radius is 0–2.',
    ),
    Tool(
        "segment_context",
        ("chapter", "index"),
        ("before", "after"),
        "Read context across chapter boundaries. Use a chapter index and its text_segments index; before/after are 0–6.",
    ),
    Tool(
        "book_context",
        ("section",),
        ("chapter",),
        "Read style_guide, book_synopsis or chapter_digest (requires chapter).",
    ),
)


def describe(tools: tuple[Tool, ...]) -> str:
    return "\n".join(tool.usage() for tool in tools)


class ToolError(ValueError):
    """A structural request error with a stable, non-sensitive reason code."""


class BoundTools:
    def __init__(self, specs: tuple[Tool, ...], **handlers: Handler):
        if set(handlers) != {s.name for s in specs} or len(handlers) != len(specs):
            raise ValueError("Tool contracts and handlers do not match")
        self._tools = {s.name: (s, handlers[s.name]) for s in specs}

    def call(self, name: object, arguments: object) -> dict[str, Any]:
        if not isinstance(name, str) or name not in self._tools:
            raise ToolError("unknown_tool")
        spec, handler = self._tools[name]
        if (
            not isinstance(arguments, dict)
            or not set(spec.required) <= arguments.keys()
            or not arguments.keys() <= set((*spec.required, *spec.optional))
        ):
            raise ToolError("invalid_arguments")
        return handler(arguments)
