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
from typing import TYPE_CHECKING

from .diagnostics import EngineDiagnostics, Reporter

if TYPE_CHECKING:
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


async def serve(workspace: Path, protocol_stream, lifecycle: EngineDiagnostics) -> None:
    with lifecycle.observe("application"):
        import uvicorn

        from .main import create_app

        app = create_app(
            workspace,
            api_token=os.environ.get("WENYI_API_TOKEN"),
            diagnostics=lifecycle.reporter,
            engine_diagnostics=lifecycle,
        )
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            with lifecycle.observe("listen"):
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
                    ready = (
                        "WENYI_READY "
                        + json.dumps({"protocol": 1, "port": port, "pid": os.getpid()})
                        + "\n"
                    )
                    if lifecycle.reporter is not None:
                        lifecycle.reporter.protocol(ready)
                    else:
                        protocol_stream.write(ready)
                        protocol_stream.flush()
                    lifecycle.ready = True
                except (BrokenPipeError, OSError):
                    server.should_exit = True
            with lifecycle.observe("running" if lifecycle.ready else "listen"):
                await task
    finally:
        with lifecycle.observe("shutdown"):
            app.state.backend.repository.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=None)
    args = parser.parse_args()
    if not os.environ.get("WENYI_API_TOKEN"):
        parser.error("WENYI_API_TOKEN must be supplied by the desktop parent")
    protocol_stream = sys.stdout
    sys.stdout = sys.stderr
    reporter = (
        Reporter(protocol_stream) if os.environ.get("WENYI_DIAGNOSTICS_PROTOCOL") == "1" else None
    )
    if reporter is not None:
        reporter.emit("engine_bootstrap")
    lifecycle = EngineDiagnostics(reporter)
    try:
        with lifecycle.observe("bootstrap"):
            asyncio.run(serve(args.data_dir or default_workspace(), protocol_stream, lifecycle))
            # ASGI can consume shutdown failures too. Do not report failure and
            # then let the interpreter return a misleading successful exit code.
            if lifecycle.failed:
                raise SystemExit(lifecycle.exit_code or 1)
    finally:
        lifecycle.stopped()
        if reporter is not None:
            reporter.close()


if __name__ == "__main__":
    main()
