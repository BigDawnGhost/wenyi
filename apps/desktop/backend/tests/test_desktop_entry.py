"""Local desktop process contract: isolated state, loopback, auth and clean exit."""

from __future__ import annotations

import asyncio
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


@pytest.mark.parametrize("shutdown", ["command", "eof"])
def test_desktop_subprocess_ready_auth_origin_and_shutdown(tmp_path: Path, shutdown):
    token = secrets.token_hex(32)
    env = {k: v for k, v in os.environ.items() if not k.startswith("WENYI_")}
    env.update(WENYI_API_TOKEN=token)
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
        text=True,
    )
    lines: queue.Queue[str] = queue.Queue()
    threading.Thread(target=lambda: lines.put(child.stdout.readline()), daemon=True).start()
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
        assert child.returncode == 0
        assert token not in child.stdout.read() + child.stderr.read()
        assert sentinel.read_text() == "This is not a Wenyi configuration."
        assert workspace.is_dir()
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


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
