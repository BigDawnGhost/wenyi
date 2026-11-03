"""Upgrade migration: the contract, and the read-only way to read an old database.

The package is imported by the migration layer rather than by the application
startup, so an unreferenced import here cannot change how the application runs.
"""

from __future__ import annotations

from .protocol import (
    LOG_SUFFIX,
    SNAPSHOT_MANIFEST,
    SNAPSHOT_SCHEMA_VERSION,
    Migration,
    VerifyReport,
    open_read_only,
)

__all__ = [
    "LOG_SUFFIX",
    "SNAPSHOT_MANIFEST",
    "SNAPSHOT_SCHEMA_VERSION",
    "Migration",
    "VerifyReport",
    "open_read_only",
]
