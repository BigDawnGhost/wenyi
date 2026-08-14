"""Legacy pipeline runtime helpers that do not belong to translation policy.

The orchestrator still owns each top-level invocation and its metrics recorder.  This
module only centralizes source identity checks so state reuse cannot accidentally trust
an old, lock-external hash snapshot.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .metrics import RunMetricsRecorder


class SourceIdentityRuntime:
    """Resolve and validate source hashes at the legacy state-consumption boundary."""

    def __init__(self, hasher: Callable[[str], str]) -> None:
        self._hasher = hasher

    def verified_sha256(
        self,
        input_path: str,
        *,
        recorder: RunMetricsRecorder | None,
    ) -> str:
        """Return a current digest, reusing a recorder snapshot only after verification.

        The recorder is invocation-owned.  Its startup hash is merely an optimization:
        whenever the cheap file signature changed, ``verify_input_sha256`` re-hashes and
        rejects mutation before any persisted state is consumed.
        """
        if recorder is not None:
            verified = recorder.verify_input_sha256(input_path)
            if verified is not None:
                return verified
        digest = self._hasher(input_path)
        if recorder is not None:
            recorder.input["sha256"] = digest
        return digest

    def initial_sha256(
        self,
        input_path: str,
        *,
        recorder: RunMetricsRecorder | None,
    ) -> str:
        """Capture the pre-parse identity, reusing this invocation's startup hash."""
        if recorder is not None:
            initial = recorder.input.get("sha256")
            if isinstance(initial, str):
                return initial
        return self._hasher(input_path)


__all__ = ["SourceIdentityRuntime"]
