"""Plugin tests use synthetic archives and mocked HTTP/processes, never BabelDOC."""

import asyncio
import hashlib
import io
import json
import stat
import threading
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from wenyi_core.pdf_bridge.client import BabeldocBridgeError
from wenyi_desktop import plugin_packages as packages
from wenyi_desktop.babeldoc_plugin import (
    DEFAULT_BRIDGE,
    BabeldocPlugin,
    child_environment,
    finish_thread,
)
from wenyi_desktop.main import create_app, create_context
from wenyi_desktop.updates import UpdateGuard


def archive(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as bundle:
        for name, value in files.items():
            bundle.writestr(name, value)
    return output.getvalue()


def asset(data, name="runtime.zip"):
    return {"name": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def manifest(runtime, models):
    return {
        "protocol": 1,
        "version": "0.1.0",
        "platforms": {"linux-x86_64": asset(runtime)},
        "models": asset(models, "models.zip"),
    }


def test_default_is_off_and_does_not_create_plugin_files(tmp_path):
    plugin = BabeldocPlugin(tmp_path)
    plugin.restore()
    assert not plugin.enabled and not plugin.root.exists()
    assert plugin.status()["phase"] == "disabled"
    assert plugin.connection("https://external.example").url == "https://external.example"
    with pytest.raises(BabeldocBridgeError, match="Settings"):
        plugin.connection(DEFAULT_BRIDGE)


def test_child_environment_excludes_provider_secrets_and_loader_state(monkeypatch):
    for name in (
        "WENYI_API_TOKEN",
        "OPENAI_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "_PYI_ARCHIVE_FILE",
        "LD_LIBRARY_PATH",
        "PYTHONPATH",
    ):
        monkeypatch.setenv(name, "must-not-inherit")
    monkeypatch.setenv("PATH", "/test/path")
    env = child_environment("ephemeral-plugin-token")
    assert env["PATH"] == "/test/path"
    assert env["WENYI_BABELDOC_TOKEN"] == "ephemeral-plugin-token"
    assert "must-not-inherit" not in env.values()


def test_plugin_process_connection_is_private_and_restarts_without_changing_saved_url(
    tmp_path, monkeypatch
):
    async def run():
        plugin = BabeldocPlugin(tmp_path)
        plugin.root.mkdir(parents=True)
        plugin.installation = "a" * 32
        plugin.enabled = True
        plugin.version = "0.1.0"
        plugin._save()
        health = asyncio.Event()
        launches = []

        async def spawn(command, env):
            exited = asyncio.Event()
            reader = asyncio.StreamReader()
            pid = 100 + len(launches)
            reader.feed_data(
                json.dumps({"protocol": 1, "pid": pid, "port": 1234 + pid}).encode() + b"\n"
            )
            child = SimpleNamespace(
                pid=pid,
                stdout=reader,
                stdin=SimpleNamespace(close=exited.set),
                wait=exited.wait,
                kill=exited.set,
            )
            launches.append((command, env, child))
            plugin._child = child

        def handler(request):
            assert (
                request.headers["Authorization"]
                == f"Bearer {launches[-1][1]['WENYI_BABELDOC_TOKEN']}"
            )
            asyncio.get_running_loop().call_soon(health.set)
            return httpx.Response(200, json={"ok": True})

        original = httpx.AsyncClient
        monkeypatch.setattr(plugin, "_spawn", spawn)
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda **kw: original(transport=httpx.MockTransport(handler), **kw),
        )
        for attempt in range(2):
            await plugin.enable()
            await asyncio.wait_for(health.wait(), 2)
            health.clear()
            assert plugin.phase == "ready"
            connection = plugin.connection(DEFAULT_BRIDGE)
            assert connection.url.endswith(str(1334 + attempt))
            assert not connection.trust_env
            saved = (plugin.root / "plugin.json").read_text()
            assert "Authorization" not in saved
            assert launches[-1][1]["WENYI_BABELDOC_TOKEN"] not in saved
            await plugin.disable()
            assert plugin._child is None and plugin.phase == "disabled"
            with pytest.raises(BabeldocBridgeError):
                plugin.connection(DEFAULT_BRIDGE)
        assert launches[0][1]["WENYI_BABELDOC_TOKEN"] != launches[1][1]["WENYI_BABELDOC_TOKEN"]

    asyncio.run(run())


@pytest.mark.parametrize(
    "name", ["../evil", "/absolute", "C:/evil", "a\\evil", "a/../evil", "a//b", "a. /b"]
)
def test_archive_rejects_unsafe_paths(tmp_path, name):
    path = tmp_path / "bad.zip"
    path.write_bytes(archive({name: b"untrusted"}))
    with pytest.raises(ValueError, match="Unsafe"):
        packages.unpack(path, tmp_path / "stage")
    assert not (tmp_path.parent / "evil").exists()


def test_archive_rejects_symlinks_and_duplicate_case(tmp_path):
    path = tmp_path / "bad.zip"
    with zipfile.ZipFile(path, "w") as bundle:
        item = zipfile.ZipInfo("link")
        item.external_attr = (stat.S_IFLNK | 0o777) << 16
        bundle.writestr(item, "../outside")
    with pytest.raises(ValueError, match="Unsafe"):
        packages.unpack(path, tmp_path / "links")
    path.write_bytes(archive({"One": b"1", "one": b"2"}))
    with pytest.raises(ValueError, match="Unsafe"):
        packages.unpack(path, tmp_path / "duplicates")


def test_download_integrity_progress_retry_and_no_partial_publication(tmp_path):
    async def run():
        content = b"synthetic-runtime"
        progress = []
        calls = []

        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(200, content=content)

        path = tmp_path / "package.zip"
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            item = asset(content)
            await packages.download(client, "0.1.0", item, path, lambda *v: progress.append(v))
            assert progress[0] == (0, len(content))
            assert progress[-1] == (len(content), len(content))
            assert path.read_bytes() == content
            await packages.download(client, "0.1.0", item, path, lambda *v: None)
            assert len(calls) == 1
            item["sha256"] = "0" * 64
            with pytest.raises(ValueError, match="checksum"):
                await packages.download(client, "0.1.0", item, path, lambda *v: None)
            assert path.read_bytes() == content
            assert not path.with_suffix(".part").exists()

    asyncio.run(run())


@pytest.mark.parametrize("field,value", [("name", "../bad.zip"), ("size", -1), ("sha256", "bad")])
def test_manifest_rejects_untrusted_asset_fields(field, value):
    data = manifest(b"runtime", b"models")
    data["models"][field] = value
    with pytest.raises(ValueError):
        packages.validate_manifest(data, "linux-x86_64")


def test_install_commits_only_complete_runtime_and_models(tmp_path, monkeypatch):
    async def run():
        plugin = BabeldocPlugin(tmp_path)
        plugin.target = "linux-x86_64"
        runtime = archive({plugin.executable_name: b"fake executable"})
        models = archive({"models/model.onnx": b"fake weights"})
        body = manifest(runtime, models)
        phases = []
        original = httpx.AsyncClient

        def handler(request):
            phases.append(plugin.phase)
            if str(request.url) == packages.MANIFEST_URL:
                return httpx.Response(200, json=body)
            return httpx.Response(
                200, content=models if request.url.path.endswith("models.zip") else runtime
            )

        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda **kw: original(transport=httpx.MockTransport(handler), **kw),
        )
        plugin.enabled = True
        await plugin._install()
        assert phases == ["checking", "runtime", "models"]
        restored = BabeldocPlugin(tmp_path)
        assert restored.installation == plugin.installation and restored.enabled
        folder = plugin.root / "installed" / plugin.installation
        assert (folder / "runtime" / plugin.executable_name).read_bytes() == b"fake executable"
        assert (folder / "cache/models/model.onnx").read_bytes() == b"fake weights"
        await plugin.disable()
        assert folder.exists()
        assert not BabeldocPlugin(tmp_path).enabled

    asyncio.run(run())


def test_failed_setup_is_retryable_and_does_not_install(tmp_path, monkeypatch):
    async def run():
        plugin = BabeldocPlugin(tmp_path)
        plugin.target = "linux-x86_64"
        monkeypatch.setattr(plugin, "_install", AsyncMock(side_effect=ValueError("private detail")))
        await plugin.enable()
        await plugin._task
        assert plugin.phase == "error" and plugin.error == "install_failed"
        assert plugin.installation is None
        assert "private detail" not in json.dumps(plugin.status())
        await plugin.enable()
        await plugin._task
        assert plugin._install.await_count == 2
        restored = BabeldocPlugin(tmp_path)
        restored.restore()
        assert restored.error == "interrupted"
        await plugin.disable()

    asyncio.run(run())


def test_cancel_waits_for_archive_writer(tmp_path):
    async def run():
        entered, release = threading.Event(), threading.Event()

        def write():
            entered.set()
            assert release.wait(5)
            (tmp_path / "complete").touch()

        task = asyncio.create_task(finish_thread(write))
        await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (tmp_path / "complete").exists()

    asyncio.run(run())


def test_private_controls_and_active_job_guard(tmp_path, monkeypatch):
    context = create_context(tmp_path, api_token="test-token")
    app = create_app(context=context)
    context.telemetry.runtime = SimpleNamespace(accepting=True, idle=True)
    plugin = context.telemetry.babeldoc
    plugin.target = "linux-x86_64"
    monkeypatch.setattr(plugin, "enable", AsyncMock())
    client = TestClient(app)
    endpoint = "/desktop/plugins/babeldoc"
    headers = {"Authorization": "Bearer test-token"}
    assert client.get(endpoint).status_code == 401
    assert client.post(endpoint + "/enable").status_code == 401
    assert client.post(endpoint + "/disable").status_code == 401
    assert (
        client.post(
            endpoint + "/enable", headers={**headers, "Origin": "https://evil.test"}
        ).status_code
        == 403
    )
    assert client.post(endpoint + "/enable", headers=headers).status_code == 202
    pid = context.repository.create_project("Busy", "en", "zh", {})
    context.repository.create_job(pid, "parse", "run")
    assert client.post(endpoint + "/disable", headers=headers).status_code == 409
    assert not app.state.update_guard.closed
    assert endpoint not in app.openapi()["paths"]
    context.repository.close()


def test_updates_cannot_interrupt_setup_or_reopen_plugin_admission(tmp_path):
    async def run():
        context = create_context(tmp_path)
        context.telemetry.runtime = SimpleNamespace(accepting=True, idle=True)
        plugin = context.telemetry.babeldoc
        plugin.phase = "models"
        guard = UpdateGuard(context.telemetry)
        with pytest.raises(HTTPException):
            await guard.prepare()
        assert not guard.closed
        await guard.prepare(allow_plugin_activity=True)
        with pytest.raises(HTTPException):
            await guard.prepare()
        with pytest.raises(HTTPException):
            await guard.cancel()
        assert guard.closed
        await guard.cancel(allow_plugin_activity=True)
        assert not guard.closed
        context.repository.close()

    asyncio.run(run())
