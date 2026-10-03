"""One adapter per wire protocol, driven entirely by declarative provider profiles."""

from .base import (
    WIRE_ADAPTERS,
    ProfileAdapter,
    credential_failure,
    merge_extra_body,
)

__all__ = [
    "WIRE_ADAPTERS",
    "ProfileAdapter",
    "credential_failure",
    "merge_extra_body",
]
