"""Private loopback server owned by the desktop process."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
import threading
from pathlib import Path

import uvicorn

ORIGINS = (
    "tauri://localhost",
    "http://tauri.localhost",
    "https://tauri.localhost",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
)


def default_workspace() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ["LOCALAPPDATA"])
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "Wenyi Desktop"


class LocalBoundary:
    """Reject DNS rebinding and foreign browser origins, including WebSockets."""

    def __init__(self, app, port: int):
        self.app = app
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    async def __call__(self, scope, receive, send):
        if scope["type"] in {"http", "websocket"}:
            headers = scope.get("headers", [])
            hosts = [v.decode("latin1") for k, v in headers if k.lower() == b"host"]
            origins = [v.decode("latin1") for k, v in headers if k.lower() == b"origin"]
            allowed = len(hosts) == 1 and hosts[0] in self.hosts
            allowed &= not origins or (len(origins) == 1 and origins[0] in ORIGINS)
            if scope["type"] == "websocket":
                allowed &= len(origins) == 1
            if not allowed:
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                else:
                    await send({"type": "http.response.start", "status": 403, "headers": []})
                    await send({"type": "http.response.body", "body": b"Forbidden local request"})
                return
        await self.app(scope, receive, send)


def watch_parent(server: uvicorn.Server) -> None:
    """A closed pipe also handles abrupt parent termination without polling."""
    for line in sys.stdin:
        try:
            if json.loads(line) == {"command": "shutdown"}:
                break
        except (ValueError, TypeError):
            continue
    server.should_exit = True
    # EOF means the owner may already be gone and cannot enforce its own deadline.
    # Let lifespan flush checkpoints first, but do not leave an orphan on a hung call.
    watchdog = threading.Timer(30, lambda: os._exit(1))
    watchdog.daemon = True
    watchdog.start()


async def serve(workspace: Path, protocol_stream) -> None:
    from .main import create_app

    app = create_app(workspace, api_token=os.environ.get("WENYI_API_TOKEN"))
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            config = uvicorn.Config(
                LocalBoundary(app, port),
                host="127.0.0.1",
                port=port,
                access_log=False,
                log_config=None,
                timeout_graceful_shutdown=25,
            )
            server = uvicorn.Server(config)
            threading.Thread(target=watch_parent, args=(server,), daemon=True).start()
            task = asyncio.create_task(server.serve(sockets=[listener]))
            while not server.started and not task.done():
                await asyncio.sleep(0.01)
            if server.started and not server.should_exit:
                try:
                    protocol_stream.write(
                        "WENYI_READY "
                        + json.dumps({"protocol": 1, "port": port, "pid": os.getpid()})
                        + "\n"
                    )
                    protocol_stream.flush()
                except (BrokenPipeError, OSError):
                    server.should_exit = True
            await task
    finally:
        app.state.backend.repository.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=None)
    args = parser.parse_args()
    if not os.environ.get("WENYI_API_TOKEN"):
        parser.error("WENYI_API_TOKEN must be supplied by the desktop parent")
    protocol_stream = sys.stdout
    sys.stdout = sys.stderr
    asyncio.run(serve(args.data_dir or default_workspace(), protocol_stream))


if __name__ == "__main__":
    main()
