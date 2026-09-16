"""Lightweight subtitle workflow independent of the book pipeline.
ingest.srt_reader and assemble.srt_writer handle I/O. This package owns state/srt run state
and concurrent sliding-window translation, without Orchestrator, glossary or review
dependencies.
"""

from .store import SrtRunStore
from .translate import translate_srt

__all__ = [
    "SrtRunStore",
    "translate_srt",
]
