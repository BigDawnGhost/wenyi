"""Desktop observations attached to the existing routed client, not its ledger."""

from __future__ import annotations

import time
from collections import OrderedDict
from threading import Lock

from wenyi_core.llm.router import RoutedLLMClient

from .diagnostics import current


class DesktopLLMClient(RoutedLLMClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # A client belongs to one task. Capture once, including for raw worker threads.
        self._diagnostics, self._diagnostic_context = current()
        self._observations: OrderedDict[str, tuple[float, int | None]] = OrderedDict()
        self._observation_lock = Lock()

    def _emit_event(self, event: str, **data) -> None:
        super()._emit_event(event, **data)
        reporter = self._diagnostics
        if reporter is None:
            return
        fields = {**self._diagnostic_context}
        call_id = data.get("call_id")
        if isinstance(call_id, str):
            fields["call_ref"] = reporter.reference(call_id)
            with self._observation_lock:
                started, tokens = self._observations.get(call_id, (time.monotonic(), None))
                sample = data.get("total_tokens")
                if type(sample) is int and sample >= 0:
                    tokens = sample
                fields["duration_ms"] = int((time.monotonic() - started) * 1000)
                fields["tokens"] = tokens
                self._observations[call_id] = (started, tokens)
                if event == "llm_request_completed":
                    self._observations.pop(call_id, None)
                while len(self._observations) > 1024:
                    self._observations.popitem(last=False)
        operation = data.get("operation")
        if isinstance(operation, str):
            fields["phase"] = operation.split(".", 1)[0]
        for key in ("attempt", "status_code"):
            fields[key] = data.get(key)
        wait = data.get("wait_seconds")
        if isinstance(wait, (float, int)) and 0 <= wait <= 86400:
            fields["wait_ms"] = int(wait * 1000)
        reporter.emit(event, **fields)
