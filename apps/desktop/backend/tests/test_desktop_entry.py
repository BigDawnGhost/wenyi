"""Local desktop process contract: isolated state, loopback, auth and clean exit."""

from __future__ import annotations

import asyncio
import io
import json
import os
import queue
import secrets
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from wenyi_desktop.desktop import ORIGINS, LocalBoundary


@pytest.mark.parametrize("transport", ["http", "websocket"])
@pytest.mark.parametrize(
    ("host", "origin", "allowed"),
    [
        ("127.0.0.1:1234", "tauri://localhost", True),
        ("localhost:1234", "http://tauri.localhost", True),
        ("evil.example:1234", "tauri://localhost", False),
        ("127.0.0.1:1234", "https://evil.example", False),
        ("127.0.0.1:1234", "null", False),
        ("127.0.0.1:4567", "tauri://localhost", False),
    ],
)
def test_local_boundary(transport, host, origin, allowed):
    calls = []
    messages = []

    async def app(scope, receive, send):
        calls.append(scope)

    async def send(message):
        messages.append(message)

    asyncio.run(
        LocalBoundary(app, 1234)(
            {
                "type": transport,
                "headers": [(b"host", host.encode()), (b"origin", origin.encode())],
            },
            None,
            send,
        )
    )
    assert bool(calls) is allowed
    if not allowed:
        assert messages[0].get("status", messages[0].get("code")) in {403, 1008}


def request(base, path, **headers):
    req = urllib.request.Request(base + path, headers=headers)
    try:
        return urllib.request.urlopen(req, timeout=5)
    except urllib.error.HTTPError as error:
        return error


def owned_python(env):
    """Mirror the native launch contract: the interpreter is the owned child."""
    if sys.platform == "win32":
        # The venv redirector uses exactly this CPython mechanism. Bypassing it
        # preserves venv imports while making Popen.kill target the real engine.
        env["__PYVENV_LAUNCHER__"] = sys.executable
        return sys._base_executable
    return sys.executable


@pytest.mark.skipif(sys.platform != "win32", reason="CPython Windows venv redirector")
def test_owned_python_preserves_venv_and_process_identity(tmp_path):
    env = os.environ.copy()
    executable = owned_python(env)
    child = subprocess.Popen(
        [
            executable,
            "-c",
            "import json, os, sys; "
            "print(json.dumps([os.getpid(), sys.executable, sys.prefix, sys.base_prefix]))",
        ],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = child.communicate(timeout=10)
        assert child.returncode == 0, stderr
        pid, executable, prefix, base_prefix = json.loads(stdout)
        assert pid == child.pid
        assert Path(executable) == Path(sys.executable)
        assert Path(prefix) == Path(sys.prefix)
        assert Path(base_prefix) == Path(sys.base_prefix)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


# One group, because each variant starts a real engine process and tears down a loopback
# server. Serialising them keeps a stalled shutdown on a single worker instead of letting
# four of them compete with unrelated tests for the same machine.
@pytest.mark.xdist_group("desktop-subprocess")
@pytest.mark.parametrize("shutdown", ["command", "eof"])
@pytest.mark.parametrize("diagnostics", [False, True])
def test_desktop_subprocess_ready_auth_origin_and_shutdown(tmp_path: Path, shutdown, diagnostics):
    token = secrets.token_hex(32)
    env = {k: v for k, v in os.environ.items() if not k.startswith("WENYI_")}
    env.update(WENYI_API_TOKEN=token)
    if diagnostics:
        env["WENYI_DIAGNOSTICS_PROTOCOL"] = "1"
    # A sentinel CLI configuration must not be read or changed.
    sentinel = tmp_path / "config.yaml"
    sentinel.write_text("This is not a Wenyi configuration.")
    workspace = tmp_path / "desktop"
    child = subprocess.Popen(
        [owned_python(env), "-m", "wenyi_desktop.desktop", "--data-dir", str(workspace)],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        text=True,
    )
    lines: queue.Queue[str] = queue.Queue()
    observations = []

    def collect():
        for line in child.stdout:
            if line.startswith("WENYI_DIAGNOSTIC "):
                observations.append(json.loads(line.removeprefix("WENYI_DIAGNOSTIC ")))
            else:
                lines.put(line)

    reader = threading.Thread(target=collect, daemon=True)
    reader.start()
    try:
        line = lines.get(timeout=30)
        assert line.startswith("WENYI_READY "), child.stderr.read() if child.poll() else line
        ready = json.loads(line.removeprefix("WENYI_READY "))
        assert set(ready) == {"protocol", "port", "pid"}
        assert ready["protocol"] == 1 and ready["pid"] == child.pid
        assert 0 < ready["port"] < 65536 and token not in line
        base = f"http://127.0.0.1:{ready['port']}"
        with request(base, "/health") as response:
            assert response.status == 200
        with request(base, "/projects") as response:
            assert response.status == 401
        with request(base, "/projects", Authorization=f"Bearer {token}") as response:
            assert response.status == 200
        for origin in ORIGINS:
            with request(base, "/health", Origin=origin) as response:
                assert response.status == 200
                assert response.headers["access-control-allow-origin"] == origin
        with request(base, "/health", Origin="https://evil.example") as response:
            assert response.status == 403
        with request(base, "/health", Host="evil.example") as response:
            assert response.status == 403
        if shutdown == "command":
            child.stdin.write('{"command":"shutdown"}\n')
            child.stdin.flush()
        else:
            child.stdin.close()
        child.wait(timeout=30)
        reader.join(timeout=5)
        assert child.returncode == 0
        assert token not in json.dumps(observations) + child.stderr.read()
        if diagnostics:
            assert observations[0]["event"] == "engine_bootstrap"
            assert observations[-1] == {
                "event": "engine_stopped",
                "attributes": {"stage": "running", "reason": "completed", "exit_code": 0},
            }
            assert any(row["event"] == "http_failed" for row in observations)
            assert str(workspace) not in json.dumps(observations)
        else:
            assert observations == []
        assert sentinel.read_text() == "This is not a Wenyi configuration."
        assert workspace.is_dir()
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


@pytest.mark.parametrize("encoding", ["cp936", "cp1252", "utf-8"])
def test_native_startup_reads_utf8_proof_with_legacy_stdin_encoding(
    tmp_path, monkeypatch, encoding
):
    from wenyi_desktop import desktop, paths
    from wenyi_desktop.ownership import MAGIC

    workspace = (tmp_path / "中文用户" / "文译").resolve()
    paths.locks(workspace).mkdir(parents=True)
    endpoint = {
        "protocol": 1,
        "pid": os.getppid(),
        "port": 12345,
        "instance": list(range(16)),
    }
    paths.instance_endpoint(workspace).write_text(json.dumps(endpoint), encoding="utf-8")
    proof = {**endpoint, "workspace": str(workspace), "token": secrets.token_hex(32)}
    wire = (json.dumps(proof, ensure_ascii=False) + '\n{"command":"shutdown"}\n').encode("utf-8")
    connection = MagicMock()
    connection.__enter__.return_value.recv.return_value = b"\x01"
    monkeypatch.setattr(
        "wenyi_desktop.ownership.socket.create_connection", lambda *args, **kwargs: connection
    )
    served = []

    async def serve(root, stream, lifecycle, **_roots):
        assert json.loads(sys.stdin.readline()) == {"command": "shutdown"}
        served.append(root)
        lifecycle.ready = True

    monkeypatch.setattr(desktop, "serve", serve)
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(
        sys, "argv", ["wenyi-engine", "--native-startup", "--data-dir", str(workspace)]
    )
    monkeypatch.setenv("WENYI_API_TOKEN", secrets.token_hex(32))
    monkeypatch.delenv("WENYI_DIAGNOSTICS_PROTOCOL", raising=False)
    with io.TextIOWrapper(io.BytesIO(wire), encoding=encoding) as stream:
        monkeypatch.setattr(sys, "stdin", stream)
        desktop.main()
    assert served == [workspace]
    connection.__enter__.return_value.sendall.assert_called_with(
        MAGIC + bytes.fromhex(proof["token"])
    )


@pytest.mark.parametrize("stage", ["application", "runtime"])
def test_startup_failure_reports_original_error_before_uvicorn_exit(tmp_path, stage):
    """A real failed lifespan must not become just bootstrap + stopped."""
    workspace = tmp_path / "fake-private-workspace"
    workspace.mkdir()
    if stage == "application":
        (workspace / "catalog.sqlite3").write_text("Not a SQLite database.")
    else:
        (workspace / "locks" / ".desktop.lock").mkdir(parents=True)
    token = secrets.token_hex(32)
    env = {k: v for k, v in os.environ.items() if not k.startswith("WENYI_")}
    env.update(WENYI_API_TOKEN=token, WENYI_DIAGNOSTICS_PROTOCOL="1")
    # Keep stdin open, just like the native parent. Capture neither arbitrary
    # stderr nor a traceback: the diagnostic protocol alone must explain failure.
    output = tmp_path / "protocol.txt"
    with output.open("w") as stream:
        child = subprocess.Popen(
            [owned_python(env), "-m", "wenyi_desktop", "--data-dir", str(workspace)],
            cwd=tmp_path,
            env=env,
            stdin=subprocess.PIPE,
            stdout=stream,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            text=True,
        )
        try:
            child.wait(timeout=20)
            assert child.returncode != 0
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
            child.stdin.close()
    protocol = output.read_text()
    assert "WENYI_READY " not in protocol
    assert token not in protocol and "fake-private" not in protocol
    observations = [
        json.loads(line.removeprefix("WENYI_DIAGNOSTIC ")) for line in protocol.splitlines()
    ]
    failures = [row for row in observations if row["event"] == "engine_bootstrap_failed"]
    assert len(failures) == 1
    assert failures[0]["attributes"]["stage"] == stage
    assert failures[0]["attributes"]["module"] in {"catalog", "local_backend", "local_runtime"}
    assert failures[0]["attributes"]["line"] > 0
    assert observations[-1] == {
        "event": "engine_stopped",
        "attributes": {
            "stage": "bootstrap",
            "reason": "failed",
            "exit_code": child.returncode,
        },
    }


def test_asgi_consumed_shutdown_failure_exits_unsuccessfully(monkeypatch, tmp_path):
    from wenyi_desktop import desktop

    # The engine also receives the three resolved roots as keyword arguments; this
    # stub only exercises the lifecycle wiring, so it accepts and ignores them.
    async def serve(workspace, stream, lifecycle, **_roots):
        lifecycle.ready = True
        # Mirror ASGI consuming a lifecycle failure instead of propagating it.
        with pytest.raises(OSError):
            with lifecycle.observe("shutdown"):
                raise OSError("fake-private-shutdown-error")

    stream = io.StringIO()
    monkeypatch.setattr(desktop, "serve", serve)
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "argv", ["wenyi-engine", "--data-dir", str(tmp_path)])
    monkeypatch.setenv("WENYI_API_TOKEN", secrets.token_hex(32))
    monkeypatch.setenv("WENYI_DIAGNOSTICS_PROTOCOL", "1")
    with pytest.raises(SystemExit) as stopped:
        desktop.main()
    assert stopped.value.code == 1
    protocol = stream.getvalue()
    observations = [
        json.loads(line.removeprefix("WENYI_DIAGNOSTIC ")) for line in protocol.splitlines()
    ]
    assert observations[1]["event"] == "engine_runtime_failed"
    assert observations[1]["attributes"]["stage"] == "shutdown"
    assert observations[-1]["attributes"] == {
        "stage": "running",
        "reason": "failed",
        "exit_code": 1,
    }
    assert "fake-private" not in protocol


def test_desktop_requires_parent_token(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "WENYI_API_TOKEN"}
    result = subprocess.run(
        [owned_python(env), "-m", "wenyi_desktop.desktop", "--data-dir", str(tmp_path / "unused")],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert not (tmp_path / "unused").exists()
    assert "WENYI_READY" not in result.stdout
