"""Zero-dependency data models shared across every layer.

Importing from here is always safe: nothing in this package reaches into another
wenyi_core subpackage or a third-party library that carries runtime behaviour. That
property lets markup, storage, glossary and pipeline name the same types without importing
each other, so the guard in `packages/core/tests/test_architecture_boundaries.py` enforces
it instead of trusting convention.
"""

from .document import KIND_HEADING, KIND_TEXT, Chapter, Document, Segment
from .errors import IngestError, MinerUError, MinerUTimeoutError
from .glossary import (
    TYPE_APPELLATION,
    TYPE_FIXED_EXPR,
    TYPE_HONORIFIC,
    TYPE_PERSON,
    TYPE_SPEECH,
    TYPE_TERM,
    GlossaryTerm,
)

__all__ = [
    "KIND_HEADING",
    "KIND_TEXT",
    "TYPE_APPELLATION",
    "TYPE_FIXED_EXPR",
    "TYPE_HONORIFIC",
    "TYPE_PERSON",
    "TYPE_SPEECH",
    "TYPE_TERM",
    "Chapter",
    "Document",
    "GlossaryTerm",
    "IngestError",
    "MinerUError",
    "MinerUTimeoutError",
    "Segment",
]
