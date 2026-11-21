"""Require a concrete node type when inspecting an exported document."""

from typing import TypeVar

T = TypeVar("T")


def require(value: object, kind: type[T]) -> T:
    assert isinstance(value, kind), f"Expected {kind.__name__}, got {type(value).__name__}"
    return value
