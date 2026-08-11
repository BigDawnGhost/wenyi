"""Framework-neutral persistence ports for immutable workflow artifacts.

The workflow domain identifies every stored payload with :class:`ArtifactRef`.
Concrete stores may use a filesystem, object storage, or another backend, but
backend paths, client objects, and open handles must never replace that stable
reference in workflow state.

This module deliberately defines contracts only.  It does not choose a storage
layout, perform workflow commits, or depend on RunStore, a CLI, or a graph
runtime.
"""

from __future__ import annotations

from typing import BinaryIO, ContextManager, Protocol

from ..domain.workflow import ArtifactRef


class ArtifactStoreError(Exception):
    """Base class for failures reported through the artifact-store boundary."""


class ArtifactNotFound(ArtifactStoreError):
    """Raised when a valid artifact reference has no stored payload."""


class ArtifactCorruption(ArtifactStoreError):
    """Raised when stored bytes disagree with an artifact reference."""


class InvalidArtifactReference(ArtifactStoreError):
    """Raised when an ``ArtifactRef`` is malformed or unsupported by a store."""


class ArtifactStore(Protocol):
    """Port for publishing and reading immutable, content-addressed artifacts.

    Implementations must treat a returned :class:`ArtifactRef` as immutable:
    publishing the same bytes again may reuse an existing object, but must not
    mutate bytes already addressable through an earlier reference.

    Methods accept or return ``ArtifactRef`` whenever an artifact crosses the
    storage boundary.  No implementation-specific path, key, or client object
    may leak into workflow state.
    """

    def put_bytes(self, data: bytes, *, media_type: str) -> ArtifactRef:
        """Publish ``data`` and return its detached immutable reference.

        The method must not retain a mutable view of caller-owned data.  A
        successful return means the referenced bytes are available for later
        verification and reading.
        """
        ...

    def put_stream(self, stream: BinaryIO, *, media_type: str) -> ArtifactRef:
        """Publish bytes from the stream's current position through EOF.

        ``stream`` need not be seekable.  The caller retains ownership of the
        input stream, so the store must not close it.
        """
        ...

    def put_json(
        self,
        value: object,
        *,
        media_type: str = "application/json",
    ) -> ArtifactRef:
        """Publish a stable JSON value using deterministic UTF-8 encoding.

        Implementations must reject values that cannot make a lossless stable
        JSON round trip, including non-finite floats and non-string mapping
        keys.  Equivalent accepted values must produce the same bytes and thus
        the same content identity.
        """
        ...

    def verify(self, ref: ArtifactRef) -> ArtifactRef:
        """Fully verify ``ref`` and return a detached normalized reference.

        Verification covers the reference shape, backend address, byte count,
        and SHA-256 digest.  Invalid references, missing objects, and content
        mismatches are reported as ``InvalidArtifactReference``,
        ``ArtifactNotFound``, and ``ArtifactCorruption`` respectively.
        """
        ...

    def open_binary(self, ref: ArtifactRef) -> ContextManager[BinaryIO]:
        """Return a context manager that opens the bytes identified by ``ref``.

        The reference must be validated and missing content must raise
        ``ArtifactNotFound``.  Opening is not a substitute for ``verify``:
        callers that require a complete digest check must verify explicitly.
        The context manager owns and closes the returned reader.
        """
        ...

    def contains(self, ref: ArtifactRef) -> bool:
        """Return whether the backend contains the object addressed by ``ref``.

        A malformed or backend-incompatible reference raises
        ``InvalidArtifactReference``.  This is an existence probe rather than a
        content-integrity check; use ``verify`` before trusting stored bytes.
        """
        ...


__all__ = [
    "ArtifactCorruption",
    "ArtifactNotFound",
    "ArtifactStore",
    "ArtifactStoreError",
    "InvalidArtifactReference",
]
