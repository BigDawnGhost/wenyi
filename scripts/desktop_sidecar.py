"""Build and smoke-test a relocatable PyInstaller onedir desktop engine.

Run with `uv run --no-project python scripts/desktop_sidecar.py`.
The freezer runs in a temporary desktop-only environment, even when invoked
from a workspace development environment. Build dependencies never enter the runtime.
"""

from __future__ import annotations

import json
import os
import queue
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from importlib.metadata import version
from importlib.util import find_spec
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DESTINATION = ROOT / "apps/desktop/sidecar"
EXCLUDED_MODULES = ("wenyi_api", "wenyi_cli", "psycopg", "psycopg_pool", "arq", "redis")


def verify_bundle(directory: Path) -> None:
    internal = directory / "_internal"
    assert internal.is_dir(), "Expected onedir _internal beside the executable"
    cache = internal / "tiktoken_cache"
    assert cache.is_dir() and any(p.is_file() and p.stat().st_size for p in cache.iterdir()), (
        "Missing bundled tiktoken vocabulary"
    )
    resources = ROOT / "packages/core/wenyi_core/i18n/data"
    expected = [p.relative_to(resources) for p in resources.rglob("*") if p.is_file()]
    assert expected
    for resource in expected:
        assert (internal / "wenyi_core/i18n/data" / resource).is_file(), resource
    for package in ("wenyi-core", "wenyi-backend", "wenyi-desktop"):
        normalized = package.replace("-", "_")
        metadata = list(internal.glob(f"{normalized}-*.dist-info/METADATA"))
        assert len(metadata) == 1, f"Missing {package} metadata"
        assert f"Version: {version(package)}\n" in metadata[0].read_text()
    for module in EXCLUDED_MODULES:
        assert not (internal / module).exists(), f"Unexpected bundled module: {module}"
        assert not list(internal.glob(f"{module}-*.dist-info")), module


def smoke_test(executable: Path) -> None:
    # Launch outside the repository and with no Python path or external services.
    with tempfile.TemporaryDirectory(prefix="wenyi-sidecar-") as temporary:
        token = secrets.token_hex(32)
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("PYTHON", "WENYI_", "TIKTOKEN", "DATA_GYM"))
            and not k.lower().endswith(("_proxy", "_api_key"))
        }
        # A refused proxy exposes accidental runtime downloads; local HTTP still works.
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            env[key] = "http://127.0.0.1:1"
        env["NO_PROXY"] = "127.0.0.1,localhost"
        env["WENYI_API_TOKEN"] = token
        with subprocess.Popen(
            [str(executable), "--data-dir", str(Path(temporary) / "workspace")],
            cwd=temporary,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ) as child:
            lines: queue.Queue[str] = queue.Queue()
            threading.Thread(target=lambda: lines.put(child.stdout.readline()), daemon=True).start()
            try:
                line = lines.get(timeout=60)
                assert line.startswith("WENYI_READY "), "Sidecar failed before ready"
                ready = json.loads(line.removeprefix("WENYI_READY "))
                assert ready["protocol"] == 1 and ready["pid"] == child.pid
                assert token not in line
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{ready['port']}/health", timeout=10
                ) as response:
                    assert response.status == 200
                smoke_parse(f"http://127.0.0.1:{ready['port']}", token)
                child.stdin.write('{"command":"shutdown"}\n')
                child.stdin.flush()
                child.wait(timeout=35)
                assert child.returncode == 0
            except BaseException:
                child.kill()
                child.wait()
                # Do not echo arbitrary process output or its environment.
                raise


def smoke_parse(base_url: str, token: str) -> None:
    """Upload synthetic text and wait for the real background parser, without an LLM."""
    boundary = secrets.token_hex(16)
    project = json.dumps(
        {"name": "Offline packaging smoke", "source_lang": "en", "target_lang": "zh"}
    )
    payload = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="project"\r\n\r\n'
        f"{project}\r\n--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="synthetic.txt"\r\n'
        "Content-Type: text/plain\r\n\r\n"
        "Chapter 1\n\nA small synthetic paragraph tests the packaged tokenizer.\n"
        f"\r\n--{boundary}--\r\n"
    ).encode()
    # Do not let the invoking shell's proxy affect loopback requests either.
    http = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    headers = {"Authorization": f"Bearer {token}"}
    request = urllib.request.Request(
        f"{base_url}/projects",
        data=payload,
        headers={**headers, "Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with http.open(request, timeout=10) as response:
        project_id = json.load(response)["id"]
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        request = urllib.request.Request(
            f"{base_url}/projects/{project_id}/preview", headers=headers
        )
        try:
            with http.open(request, timeout=10) as response:
                preview = json.load(response)
            assert preview["fmt"] == "text"
            assert preview["chapter_count"] > 0 and preview["total_word_count"] > 0
            return
        except urllib.error.HTTPError as error:
            if error.code != 409:
                raise
        with http.open(
            urllib.request.Request(f"{base_url}/projects/{project_id}", headers=headers),
            timeout=10,
        ) as response:
            state = json.load(response)
        assert state["status"] != "error", "Packaged background parse failed"
        time.sleep(0.1)
    raise AssertionError("Packaged background preview did not become ready")


def build() -> None:
    for module in EXCLUDED_MODULES:
        assert find_spec(module) is None, f"Build environment is not isolated: {module}"
    expected_version = os.environ.get("WENYI_BUILD_PYTHON_VERSION")
    if expected_version:
        for package in ("wenyi-core", "wenyi-backend", "wenyi-desktop"):
            assert version(package) == expected_version, f"{package} version drift"
    # Use the isolated build venv, never a developer's already-warm cache.
    cache = Path(sys.prefix) / "share/tiktoken"
    assert not cache.exists(), "Expected a clean build vocabulary cache"
    os.environ["TIKTOKEN_CACHE_DIR"] = str(cache)
    import tiktoken
    from wenyi_core.ingest.tokens import ENCODING_NAME

    tiktoken.get_encoding(ENCODING_NAME)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--onedir",
            "--name",
            "wenyi-engine",
            "--distpath",
            str(DESTINATION),
            "--workpath",
            str(ROOT / ".scratch/desktop-pyinstaller"),
            "--specpath",
            str(ROOT / ".scratch"),
            "--copy-metadata",
            "wenyi-core",
            "--copy-metadata",
            "wenyi-backend",
            "--copy-metadata",
            "wenyi-desktop",
            "--collect-data",
            "wenyi_core",
            "--collect-submodules",
            "wenyi_core.llm.providers",
            "--collect-submodules",
            "uvicorn",
            "--collect-submodules",
            "tiktoken_ext",
            "--collect-submodules",
            "keyring.backends",
            "--add-data",
            f"{cache}{os.pathsep}tiktoken_cache",
            *(argument for module in EXCLUDED_MODULES for argument in ("--exclude-module", module)),
            str(ROOT / "scripts/desktop_entry.py"),
        ],
        check=True,
        cwd=ROOT,
    )
    directory = DESTINATION / "wenyi-engine"
    verify_bundle(directory)
    smoke_test(directory / ("wenyi-engine.exe" if sys.platform == "win32" else "wenyi-engine"))
    print("Desktop sidecar: metadata, resources, offline TXT parse/preview and shutdown verified.")


def main() -> None:
    if sys.argv[1:] == ["--isolated-build"]:
        build()
        return
    if sys.argv[1:]:
        raise SystemExit("Usage: python scripts/desktop_sidecar.py")
    with tempfile.TemporaryDirectory(prefix="wenyi-desktop-build-") as temporary:
        environment = Path(temporary) / "venv"
        env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
        env["UV_PROJECT_ENVIRONMENT"] = str(environment)
        subprocess.run(
            [
                "uv",
                "sync",
                "--locked",
                "--package",
                "wenyi-desktop",
                "--no-dev",
                "--no-editable",
                # Local wheel cache keys do not include every source/tag change.
                # Always freeze the current workspace, not an earlier cached wheel.
                "--reinstall-package",
                "wenyi-core",
                "--reinstall-package",
                "wenyi-backend",
                "--reinstall-package",
                "wenyi-desktop",
                "--python",
                sys.executable,
            ],
            check=True,
            cwd=ROOT,
            env=env,
        )
        python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        subprocess.run(
            ["uv", "pip", "install", "--python", str(python), "pyinstaller==6.19.0"],
            check=True,
            cwd=ROOT,
            env=env,
        )
        subprocess.run(
            [str(python), str(Path(__file__).resolve()), "--isolated-build"],
            check=True,
            cwd=ROOT,
            env=env,
        )


if __name__ == "__main__":
    main()
