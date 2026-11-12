"""Parsing errors callable from `ingest` and `pdf_bridge` without either importing the other.

`WenyiError` is imported from its own top-level module rather than re-derived here, so a
single `except WenyiError` in the CLI still catches every failure in the product.
"""

from __future__ import annotations

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
