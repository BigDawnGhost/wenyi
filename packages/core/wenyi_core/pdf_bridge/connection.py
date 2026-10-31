"""Scoped HTTP connection resolution for hosts that own an external bridge.

Persisted document metadata keeps the logical URL. Ephemeral ports and credentials
are resolved only at the HTTP boundary and never enter configuration or snapshots.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass(frozen=True)
class BridgeConnection:
    url: str
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    trust_env: bool = True


BridgeResolver = Callable[[str], BridgeConnection]
_resolver: ContextVar[BridgeResolver | None] = ContextVar("babeldoc_connection", default=None)


def resolve_connection(url: str) -> BridgeConnection:
    resolver = _resolver.get()
    return resolver(url) if resolver else BridgeConnection(url)


@contextmanager
def use_bridge_connection(resolver: BridgeResolver | None) -> Iterator[None]:
    token = _resolver.set(resolver)
    try:
        yield
    finally:
        _resolver.reset(token)
