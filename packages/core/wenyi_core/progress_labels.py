"""English wording for the stable progress keys the pipeline emits.

Pipeline progress callbacks carry message keys such as ``progress.stage.parsing``
or ``progress.stage.arbitration:2`` so that graphical clients can translate them.
Terminal output and machine-readable progress files have no translator, so they
render the keys with this table instead.
"""

from __future__ import annotations

PROGRESS_KEY_PREFIX = "progress.stage."

_ENGLISH = {
    "parsing": "Parsing document…",
    "parsed": "Source preview ready",
    "taskStart": "Task started",
    "taskDone": "Task complete",
    "locateSource": "Locating source…",
    "locateProgress": "Locating translation progress…",
    "language": "Detecting language…",
    "style": "Analyzing book style…",
    "synopsis": "Generating whole-book synopsis…",
    "prescan": "Prescanning chapter digests",
    "compilingBible": "Compiling Book Bible…",
    "consistencySweep": "Running cross-chapter consistency sweep…",
    "translationDone": "Translation complete",
    "assembling": "Assembling translation…",
    "reviewChapters": "Loading review chapters",
    "reviewCheckpoint": "Restoring review checkpoint…",
    "reviewPrepare": "Preparing review R{n}…",
    "reviewRound1": "Whole-book review R1",
    "reviewRound": "Blind whole-book review R{n}",
    "arbitration": "Conflict arbitration R{n}",
    "shadowRevision": "Shadow revision R{n}",
    "publishingAutofix": "Publishing Review Autofix",
    "chapter": "Chapter {n}",
}


def english_progress_label(label: str) -> str:
    """Render a progress key in English; return any other label unchanged."""
    head, sep, tail = label.partition(" · ")
    if not head.startswith(PROGRESS_KEY_PREFIX):
        return label
    name, _, argument = head[len(PROGRESS_KEY_PREFIX) :].partition(":")
    if name == "reviewRound" and argument == "1":
        rendered = _ENGLISH["reviewRound1"]
    else:
        template = _ENGLISH.get(name)
        if template is None:
            return label
        rendered = template.replace("{n}", argument)
    return f"{rendered}{sep}{tail}" if sep else rendered
