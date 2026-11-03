"""Narrow execution port that hides which orchestration the shared backend drives.

The backend decides which workflow to run; it never names the facade class itself.
`bindings.py` is the only composition boundary allowed to know the concrete
implementation, so swapping or sandboxing it touches one module instead of every caller.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from wenyi_core.events import ProgressFn
from wenyi_core.storage.protocol import Storage


class WorkflowRunner(Protocol):
    """One book workflow, already bound to its configuration, client and storage."""

    def prepare_for_translation(
        self, input_path: str, *, progress: ProgressFn | None = None
    ) -> Storage: ...

    def run(
        self,
        input_path: str,
        *,
        only_chapter: int | None = None,
        progress: ProgressFn | None = None,
    ) -> Storage: ...

    def run_review(
        self, input_path: str, *, progress: ProgressFn | None = None
    ) -> dict[str, Any]: ...

    def run_steps(
        self,
        input_path: str,
        steps: set[str],
        *,
        progress: ProgressFn | None = None,
    ) -> dict[str, Any]: ...


#: Binds configuration, model client and storage to one runner instance.
WorkflowRunnerFactory = Callable[..., WorkflowRunner]
