"""Parsing errors callable from `ingest` and `pdf_bridge` without either importing the other.

`WenyiError` is imported from its own top-level module rather than re-derived here, so a
single `except WenyiError` in the CLI still catches every failure in the product.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import WenyiError


class IngestError(WenyiError, RuntimeError):
    """An input-processing error suitable for direct CLI display.

    Keeps ``RuntimeError`` so existing ``except RuntimeError`` guards and tests
    continue to catch it during the migration; ``WenyiError`` adds the stable
    code shared across layers.
    """


class MinerUError(IngestError):
    """A MinerU request, parsing or result-processing failure."""


class MinerUTimeoutError(MinerUError):
    """A MinerU task that did not finish within its time limit."""


@dataclass(frozen=True)
class ExportProductOutcome:
    """Result of one product (monolingual/bilingual) inside a multi-product export.

    ``output`` holds the published path on success; ``error`` holds the failure
    description otherwise. Exactly one of the two is set.
    """

    product: str
    out_format: str
    output: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class ExportPartialError(WenyiError):
    """A multi-product export finished with at least one failed product.

    Carries the per-product manifest in ``outcomes`` and the successfully
    exported paths in ``outputs``, so callers can report partial results
    instead of losing them when one product fails.
    """

    code = "export_partial"

    def __init__(self, outcomes: list[ExportProductOutcome]) -> None:
        self.outcomes: list[ExportProductOutcome] = list(outcomes)
        self.outputs: list[str] = [
            outcome.output for outcome in self.outcomes if outcome.ok and outcome.output
        ]
        super().__init__(self._format_message())

    def _format_message(self) -> str:
        lines = ["Export finished with failures:"]
        for outcome in self.outcomes:
            status = f"success -> {outcome.output}" if outcome.ok else f"failed ({outcome.error})"
            lines.append(f"- {outcome.product} {outcome.out_format}: {status}")
        return "\n".join(lines)
