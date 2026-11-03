"""Base exception carrying a stable code for cross-layer classification."""

from __future__ import annotations

from typing import ClassVar


class WenyiError(Exception):
    """Base for Wenyi's own failures, with a stable machine-readable code.

    Subclasses declare ``code`` and ``retryable`` as class attributes so a
    caller can branch on them without importing the concrete class.

    The inherited base classes stay in place for now: ``IngestError`` keeps
    ``RuntimeError`` and the busy errors keep ``BlockingIOError``, so existing
    ``except`` clauses keep working while call sites migrate. Removing those
    builtin parents is a later step, not part of introducing this base.
    """

    code: str = "wenyi_error"
    retryable: ClassVar[bool] = False

    def __init__(self, message: str = "", *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            # An instance-level override must not leak onto the class.
            self.code = code

    def __str__(self) -> str:
        return self.message


class ArtifactCorrupt(WenyiError):
    """A stored artifact exists but cannot be decoded.

    Corruption is never reported as a missing key: callers treat a missing
    artifact as "recompute this", and for a paid model product that silently
    bills the user twice. Callers let this propagate so a person can decide
    whether to delete the key and rerun.
    """

    code = "artifact_corrupt"


class SchemaTooNew(WenyiError):
    """A database was written by a build that knows a later schema.

    Opening it read-only would be fine; opening it the way this build opens
    every database is not. The build defines its schema with "create if not
    exists", so a newer table stays and a newer column is silently absent from
    the rows this build writes. Refusing the open is what keeps a downgrade
    from leaving a store that neither version can read.

    Callers must not treat this as corruption: nothing is damaged, and the fix
    is to run the version that created the file.
    """

    code = "database_schema_too_new"
