"""Tiny helpers that keep API integration tests honest under pyright."""

from __future__ import annotations

from typing import TypeVar

T = TypeVar("T")


def must(value: T | None) -> T:
    """Narrow Optional values after DAL lookups that tests already assume succeed."""
    assert value is not None
    return value
