"""Operation IDs and tier names, importable without the routing registry.

`llm.configuration` validates models and routes while `config.py` builds an `LLMConfig`,
so validation only needs to know which IDs exist -- not their routing metadata. Keeping the
names in a module that imports nothing lets both sides name them without a cycle: the
registry in `llm.operations` imports this module, and so does `config.py`.

The IDs are data, not policy. `llm.operations` owns what each one means.
"""

from __future__ import annotations

# Selection tiers every route resolves through, strongest first.
TIERS = ("strong", "cheap", "fast")

# Every operation that can be routed to a model. Adding one here does nothing on its own:
# it still has to be registered with its workflow, flags and protocol version.
OPERATION_NAMES = (
    "language.detect",
    "analysis.style",
    "synopsis.chapter",
    "synopsis.book",
    "translation.body",
    "translation.title",
    "polish.body",
    "glossary.extract",
    "glossary.align_history",
    "terminology.discover",
    "terminology.evidence",
    "terminology.merge",
    "annotation.align",
    "review.scan",
    "review.verify",
    "review.arbitrate",
    "review.fix",
    "autofix.verify",
    "autofix.fix",
)


def require_operation_name(operation: str) -> str:
    """Reject an unknown operation before a caller looks up its route."""
    if operation not in OPERATION_NAMES:
        raise ValueError(f"Unknown model operation: {operation}")
    return operation


__all__ = ["OPERATION_NAMES", "TIERS", "require_operation_name"]
