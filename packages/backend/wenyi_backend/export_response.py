"""Bounded export streaming with deterministic cleanup on ASGI disconnect."""

from __future__ import annotations

import os
from typing import BinaryIO

from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send


class ExportResponse(StreamingResponse):
    def __init__(
        self,
        stream: BinaryIO,
        *,
        media_type: str = "application/octet-stream",
        headers: dict[str, str] | None = None,
    ):
        self.stream = stream

        def chunks():
            with stream:
                while chunk := stream.read(64 * 1024):
                    yield chunk

        try:
            super().__init__(
                chunks(),
                media_type=media_type,
                headers={
                    **(headers or {}),
                    "content-length": str(os.fstat(stream.fileno()).st_size),
                },
            )
        except BaseException:
            stream.close()
            raise

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # ASGI 2.4 disconnect skips background tasks and can suspend the iterator.
            self.stream.close()
