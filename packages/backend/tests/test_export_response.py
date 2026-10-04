"""Export cleanup completes off the event loop, even before streaming starts."""

from io import BufferedReader
from threading import get_ident

import anyio
import pytest
from starlette.requests import ClientDisconnect
from wenyi_backend.export_response import ExportResponse


@pytest.mark.parametrize("failure", ["disconnect", "cancel"])
def test_cleanup_before_first_chunk_is_off_loop_and_cancellation_safe(tmp_path, failure):
    file = tmp_path / "export.txt"
    file.write_bytes(b"translation")
    closed_on = []

    class Stream(BufferedReader):
        def close(self):
            if not self.closed:
                closed_on.append(get_ident())
            super().close()

    stream = Stream(file.open("rb"))
    response = ExportResponse(stream)

    async def run():
        loop_thread = get_ident()

        async def receive():
            raise AssertionError("ASGI 2.4 does not need a disconnect listener")

        with anyio.CancelScope() as scope:

            async def send(message):
                assert message["type"] == "http.response.start"
                if failure == "disconnect":
                    raise OSError("Client disconnected")
                scope.cancel()
                await anyio.sleep(0)

            if failure == "disconnect":
                with pytest.raises(ClientDisconnect):
                    await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
            else:
                await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        assert stream.closed
        assert len(closed_on) == 1
        assert closed_on[0] != loop_thread

    try:
        anyio.run(run)
    finally:
        stream.close()
