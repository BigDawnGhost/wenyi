"""User-visible exceptions for document parsing and preprocessing."""

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
