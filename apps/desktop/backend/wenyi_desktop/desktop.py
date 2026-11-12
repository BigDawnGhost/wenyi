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
from .ownership import NativeOwner, profile_lock

if TYPE_CHECKING:
    import uvicorn

ORIGINS = (
    "tauri://localhost",
    "http://tauri.localhost",
    "https://tauri.localhost",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
)


LOCATION_ERROR = (
    "Wenyi could not determine where to keep its files on this computer. "
    "Check the user profile environment and reopen Wenyi."
)


def local_app_data() -> Path:
    """The local application data directory, however this process can find it.

    `%LOCALAPPDATA%` is a convenience variable rather than the definition. A
    process started without the user environment -- a scheduled task, a service,
    an elevated shell, a shortcut that clears its environment block -- inherits
    no value at all, and reading the dictionary raised `KeyError` instead of
    resolving the path. `SHGetKnownFolderPath` is the definition, and it answers
    in exactly those processes.

    An absolute environment override is read first, matching the native parent.
    """
    configured = os.environ.get("LOCALAPPDATA", "").strip()
    if configured and Path(configured).is_absolute():
        return Path(configured)
    return _known_local_app_data()


def _known_local_app_data() -> Path:
    """Ask the shell for FOLDERID_LocalAppData, or explain why it cannot."""
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [
            ("data1", wintypes.DWORD),
            ("data2", wintypes.WORD),
            ("data3", wintypes.WORD),
            ("data4", ctypes.c_ubyte * 8),
        ]

    folder = GUID(
        0xF1B32785,
        0x6FBA,
        0x4FCF,
        (ctypes.c_ubyte * 8)(0x9D, 0x55, 0x7B, 0x8E, 0x7F, 0x15, 0x70, 0x91),
    )
    shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]
    shell32.SHGetKnownFolderPath.argtypes = [
        ctypes.POINTER(GUID),
        wintypes.DWORD,
        wintypes.HANDLE,
        ctypes.POINTER(ctypes.c_wchar_p),
    ]
    shell32.SHGetKnownFolderPath.restype = ctypes.c_long
    result = ctypes.c_wchar_p()
    status = shell32.SHGetKnownFolderPath(ctypes.byref(folder), 0, None, ctypes.byref(result))
    try:
        if status < 0 or not result.value:
            raise RuntimeError(LOCATION_ERROR)
        path = Path(result.value)
    finally:
        # The shell owns the allocation and handing it back is the caller's job,
        # including when the value turned out to be unusable.
        if result.value:
            ctypes.windll.ole32.CoTaskMemFree(result)  # type: ignore[attr-defined]
    if not path.is_absolute():
        raise RuntimeError(LOCATION_ERROR)
    return path


def default_workspace() -> Path:
    """Fallback for running the engine as a standalone CLI.

    The desktop parent always injects --data-dir from its own path resolution,
    so this is never the application's path: it only exists so that a developer
    can start the engine by hand. Both sides must keep resolving the same value
    (a Rust test pins them together).
    """
    if sys.platform == "win32":
        base = local_app_data()
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        configured = os.environ.get("XDG_DATA_HOME", "").strip()
        base = (
            Path(configured)
            if configured and Path(configured).is_absolute()
            else Path.home() / ".local" / "share"
        )
    return base / ("wenyi" if sys.platform not in {"win32", "darwin"} else "Wenyi") / "Desktop"


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


async def serve(
    workspace: Path,
    protocol_stream,
    lifecycle: EngineDiagnostics,
    *,
    install_root: Path | None = None,
) -> None:
    with lifecycle.observe("application"):
        import uvicorn

        from .main import create_app

        app = create_app(
            workspace,
            api_token=os.environ.get("WENYI_API_TOKEN"),
            diagnostics=lifecycle.reporter,
            engine_diagnostics=lifecycle,
            install_root=install_root,
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
    parser.add_argument("--native-startup", action="store_true", help=argparse.SUPPRESS)
    # The installation the chosen library belongs to. Refusing a location that is
    # inside it, or that contains it, needs both, and the engine must not derive
    # the installation directory from its own executable: when it runs as a
    # sidecar that path is the bundled runtime, not the application.
    parser.add_argument("--install-root", type=Path, default=None)
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
            if args.native_startup:
                # The native pipe uses UTF-8 regardless of the Windows locale.
                sys.stdin.reconfigure(encoding="utf-8")
            # An injected directory is authoritative; the fallback is CLI-only.
            workspace = (args.data_dir or default_workspace()).expanduser().resolve()
            owner = NativeOwner.read(sys.stdin, workspace) if args.native_startup else None
            with profile_lock(workspace, owner):
                asyncio.run(
                    serve(
                        workspace,
                        protocol_stream,
                        lifecycle,
                        install_root=args.install_root,
                    )
                )
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
