"""User-visible exceptions for document parsing and preprocessing."""


class IngestError(RuntimeError):
    """An input-processing error suitable for direct CLI display."""


class MinerUError(IngestError):
    """A MinerU request, parsing or result-processing failure."""


class MinerUTimeoutError(MinerUError):
    """A MinerU task that did not finish within its time limit."""
