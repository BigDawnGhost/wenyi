"""Stable machine-readable contracts shared by every prompt bundle.

Natural-language instructions belong to the target-language bundles.  This
module deliberately contains only field names and JSON examples that callers
parse programmatically.
"""

from __future__ import annotations


LANGUAGE_DETECTION_EXAMPLE = (
    '{"language":"<ISO 639-1 code such as ja/en/ru/ko/fr/de/zh>"}'
)
