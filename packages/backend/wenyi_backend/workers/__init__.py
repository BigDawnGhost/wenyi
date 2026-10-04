"""Task submission through the owning application's queue."""

from ..context import current_context


async def enqueue(name: str, **kwargs):
    return await current_context().enqueue(name, **kwargs)
