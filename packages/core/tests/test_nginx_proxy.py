"""Exercise proxy routing across API address changes in an isolated Docker network."""

from __future__ import annotations

import http.client
import json
import os
import subprocess
import time
import uuid
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("WENYI_TEST_DOCKER") != "1",
        reason="Set WENYI_TEST_DOCKER=1 to enable isolated Docker proxy tests",
    ),
]

NGINX_CONFIG = Path(__file__).resolve().parents[3] / "docker" / "nginx.conf"


def _docker(*args: str) -> str:
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=30, check=False
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def proxy_stack(tmp_path):
    nginx_image = os.environ.get("WENYI_TEST_NGINX_IMAGE", "nginx:alpine")
    python_image = os.environ.get("WENYI_TEST_PYTHON_IMAGE", "python:3.12-slim")
    for image in (nginx_image, python_image):
        _docker("image", "inspect", "--format", "{{.Id}}", image)
    backend_file = tmp_path / "backend.py"
    backend_file.write_text(
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        "import sys\n"
        "class Handler(BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        websocket = self.headers.get('Upgrade', '').lower() == 'websocket'\n"
        "        self.send_response(101 if websocket else 200)\n"
        "        self.send_header('X-Test-Origin', sys.argv[1])\n"
        "        self.send_header('X-Test-Path', self.path)\n"
        "        if websocket:\n"
        "            self.send_header('Upgrade', 'websocket')\n"
        "            self.send_header('Connection', 'upgrade')\n"
        "        self.end_headers()\n"
        "    def log_message(self, *args):\n"
        "        pass\n"
        "HTTPServer(('0.0.0.0', 8000), Handler).serve_forever()\n"
    )
    network = f"wenyi-proxy-test-{uuid.uuid4().hex[:12]}"
    containers: list[str] = []
    _docker("network", "create", network)

    def start_backend(origin: str) -> str:
        container = _docker(
            "run",
            "--detach",
            "--pull=never",
            "--network",
            network,
            "--network-alias",
            "api",
            "--mount",
            f"type=bind,source={backend_file},target=/backend.py,readonly",
            python_image,
            "python",
            "/backend.py",
            origin,
        )
        containers.append(container)
        return container

    def replace_backend(container: str) -> None:
        address = _docker(
            "inspect",
            "--format",
            "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
            container,
        )
        _docker("rm", "--force", container)
        containers.remove(container)
        # Occupy the old address so Docker must assign the replacement a new one.
        blocker = _docker(
            "run",
            "--detach",
            "--pull=never",
            "--network",
            network,
            "--ip",
            address,
            python_image,
            "python",
            "-c",
            "import time; time.sleep(120)",
        )
        containers.append(blocker)
        replacement = start_backend("replacement")
        new_address = _docker(
            "inspect",
            "--format",
            "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
            replacement,
        )
        assert address != new_address

    try:
        backend = start_backend("original")
        proxy = _docker(
            "run",
            "--detach",
            "--pull=never",
            "--network",
            network,
            "--publish",
            "127.0.0.1::80",
            "--mount",
            f"type=bind,source={NGINX_CONFIG},target=/etc/nginx/conf.d/default.conf,readonly",
            nginx_image,
        )
        containers.append(proxy)
        ports = json.loads(_docker("inspect", "--format", "{{json .NetworkSettings.Ports}}", proxy))
        port = int(ports["80/tcp"][0]["HostPort"])
        yield port, lambda: replace_backend(backend)
    finally:
        for container in reversed(containers):
            _docker("rm", "--force", container)
        _docker("network", "rm", network)


def _request(port: int, path: str, *, websocket: bool = False):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        headers = {"Upgrade": "websocket", "Connection": "Upgrade"} if websocket else {}
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        return response.status, dict(response.getheaders())
    finally:
        connection.close()


def _wait_for_backend(port: int, origin: str) -> None:
    deadline = time.monotonic() + 20
    last_result = None
    while time.monotonic() < deadline:
        try:
            last_result = _request(port, "/api/health?probe=1")
            if last_result[0] == 200 and last_result[1].get("X-Test-Origin") == origin:
                return
        except (OSError, http.client.HTTPException):
            pass
        time.sleep(0.2)
    pytest.fail(f"Proxy did not reach {origin} backend: {last_result}")


def test_api_and_websocket_recover_after_api_address_changes(proxy_stack):
    port, replace_backend = proxy_stack
    for origin in ("original", "replacement"):
        _wait_for_backend(port, origin)
        status, headers = _request(port, "/api/projects?limit=2")
        assert status == 200
        assert headers["X-Test-Origin"] == origin
        assert headers["X-Test-Path"] == "/projects?limit=2"
        status, headers = _request(port, "/ws/projects/test/progress?probe=1", websocket=True)
        assert status == 101
        assert headers["X-Test-Origin"] == origin
        assert headers["X-Test-Path"] == "/ws/projects/test/progress?probe=1"
        assert headers["Upgrade"] == "websocket"
        if origin == "original":
            replace_backend()
