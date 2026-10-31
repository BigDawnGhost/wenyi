"""Manual plugin upgrades use temporary installations and mock HTTP/processes."""

import asyncio
import hashlib
import io
import json
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from wenyi_desktop import plugin_packages as packages
from wenyi_desktop.babeldoc_plugin import DEFAULT_BRIDGE
from wenyi_desktop.main import create_app, create_context


def archive(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as bundle:
        for name, value in files.items():
            bundle.writestr(name, value)
    return output.getvalue()


def asset(content, name):
    return {"name": name, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}


def harness(tmp_path, monkeypatch, *, enabled=True):
    context = create_context(tmp_path, api_token="test-token")
    context.telemetry.runtime = SimpleNamespace(accepting=True, idle=True)
    app = create_app(context=context)
    plugin = context.telemetry.babeldoc
    plugin.target = "linux-x86_64"
    plugin.installation, plugin.version, plugin.enabled = "a" * 32, "0.1.0", enabled
    old = plugin.root / "installed" / plugin.installation
    (old / "runtime").mkdir(parents=True)
    (old / "runtime" / plugin.executable_name).write_bytes(b"old runtime")
    session = plugin.root / "sessions" / "existing" / "session.json"
    session.parent.mkdir(parents=True)
    session.write_text("untouched session", encoding="utf-8")
    plugin._save()
    runtime = archive({plugin.executable_name: b"new runtime"})
    models = archive({"models/model.onnx": b"synthetic weights"})
    state = SimpleNamespace(
        context=context,
        plugin=plugin,
        app=app,
        guard=app.state.update_guard,
        old=old,
        session=session,
        launches=[],
        requests=[],
        ready=asyncio.Event(),
        download_entered=asyncio.Event(),
        download_release=asyncio.Event(),
        health_entered=asyncio.Event(),
        health_release=asyncio.Event(),
        pause_download=False,
        pause_health=False,
        fail_new=False,
        fail_old=False,
        fail_check=False,
        manifest={
            "protocol": 1,
            "version": "0.1.1",
            "platforms": {"linux-x86_64": asset(runtime, "runtime.zip")},
            "models": asset(models, "models.zip"),
        },
    )

    async def spawn(command, env):
        exited = asyncio.Event()
        reader = asyncio.StreamReader()
        pid = 100 + len(state.launches)
        reader.feed_data(json.dumps({"protocol": 1, "pid": pid, "port": 1200 + pid}).encode())
        reader.feed_data(b"\n")
        child = SimpleNamespace(
            pid=pid,
            stdout=reader,
            stdin=SimpleNamespace(close=exited.set),
            wait=exited.wait,
            kill=exited.set,
        )
        state.launches.append((Path(command[0]).parent.parent.name, env, child))
        plugin._child = child

    async def handler(request):
        state.requests.append(str(request.url))
        if str(request.url) == packages.MANIFEST_URL:
            return (
                httpx.Response(503)
                if state.fail_check
                else httpx.Response(200, json=state.manifest)
            )
        if request.url.path == "/health":
            identity, env, _child = state.launches[-1]
            assert request.headers["Authorization"] == f"Bearer {env['WENYI_BABELDOC_TOKEN']}"
            if identity != "a" * 32:
                # No active pointer is changed before readiness and health checks.
                assert json.loads((plugin.root / "plugin.json").read_text())["version"] == "0.1.0"
                state.health_entered.set()
                if state.pause_health:
                    await state.health_release.wait()
                if state.fail_new:
                    return httpx.Response(503)
            elif state.fail_old and len(state.launches) > 1:
                return httpx.Response(503)
            asyncio.get_running_loop().call_soon(state.ready.set)
            return httpx.Response(200, json={"ok": True})
        if request.url.path.endswith("runtime.zip"):
            state.download_entered.set()
            if state.pause_download:
                await state.download_release.wait()
            return httpx.Response(200, content=runtime)
        assert request.url.path.endswith("models.zip")
        return httpx.Response(200, content=models)

    state.client = httpx.AsyncClient
    monkeypatch.setattr(plugin, "_spawn", spawn)
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: state.client(transport=httpx.MockTransport(handler), **kw),
    )
    return state


async def start_and_check(state):
    if state.plugin.enabled:
        await state.plugin.enable()
        await asyncio.wait_for(state.ready.wait(), 2)
        assert state.plugin.phase == "ready"
    await state.plugin.check_update()
    await state.plugin._operation
    assert state.plugin.status()["update_available"]


async def cleanup(state):
    await state.plugin.stop()
    state.context.repository.close()


def test_stable_plugin_versions_compare_numerically():
    assert packages.version_key("0.1.10") > packages.version_key("0.1.9")
    assert packages.version_key("1.0.0") > packages.version_key("0.99.99")
    for invalid in ("0.1", "0.1.1-beta", "01.2.3", "v0.1.1"):
        with pytest.raises(ValueError):
            packages.version_key(invalid)


@pytest.mark.parametrize("latest,available", [("0.1.10", True), ("0.1.0", False), ("0.0.9", False)])
def test_check_is_manual_numeric_and_never_downgrades(tmp_path, monkeypatch, latest, available):
    async def run():
        state = harness(tmp_path, monkeypatch)
        state.manifest["version"] = latest
        plugin = state.plugin
        try:
            await plugin.enable()
            await asyncio.wait_for(state.ready.wait(), 2)
            assert packages.MANIFEST_URL not in state.requests
            before = (plugin.root / "plugin.json").read_bytes()
            await plugin.check_update()
            await plugin._operation
            assert plugin.status()["update_available"] is available
            assert plugin.latest_version == latest and plugin.version == "0.1.0"
            assert (plugin.root / "plugin.json").read_bytes() == before
            if not available:
                with pytest.raises(ValueError):
                    await plugin.update(state.guard)
            state.fail_check = True
            await plugin.check_update()
            await plugin._operation
            assert plugin.update_error == "check_failed" and plugin.phase == "ready"
            assert not plugin.update_available
        finally:
            await cleanup(state)

    asyncio.run(run())


@pytest.mark.parametrize("enabled", [True, False])
def test_update_selects_only_healthy_candidate_and_preserves_enabled_state(
    tmp_path, monkeypatch, enabled
):
    async def run():
        state = harness(tmp_path, monkeypatch, enabled=enabled)
        plugin = state.plugin
        try:
            await start_and_check(state)
            await plugin.update(state.guard)
            await plugin._operation
            assert plugin.version == "0.1.1" and plugin.installation != "a" * 32
            assert plugin.phase == ("ready" if enabled else "disabled")
            assert plugin.enabled is enabled and not plugin.update_available
            saved = json.loads((plugin.root / "plugin.json").read_text())
            assert saved["version"] == "0.1.1" and saved["enabled"] is enabled
            assert state.old.is_dir() and state.session.read_text() == "untouched session"
            assert not state.guard.closed and not plugin.updating and plugin.update_error is None
            assert len(state.launches) == (2 if enabled else 1)
            for _identity, env, _child in state.launches:
                assert env["WENYI_BABELDOC_TOKEN"] not in json.dumps(saved)
        finally:
            await cleanup(state)

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["health", "save", "rollback"])
def test_failed_update_keeps_old_pointer_and_rolls_back(tmp_path, monkeypatch, failure):
    async def run():
        state = harness(tmp_path, monkeypatch)
        plugin = state.plugin
        try:
            await start_and_check(state)
            before = (plugin.root / "plugin.json").read_bytes()
            if failure in {"health", "rollback"}:
                state.fail_new = True
                state.fail_old = failure == "rollback"
            else:
                original = plugin._save

                def save():
                    if plugin.installation != "a" * 32:
                        raise OSError("synthetic disk failure")
                    original()

                monkeypatch.setattr(plugin, "_save", save)
            await plugin.update(state.guard)
            await plugin._operation
            assert plugin.installation == "a" * 32 and plugin.version == "0.1.0"
            assert (plugin.root / "plugin.json").read_bytes() == before
            assert state.old.exists() and state.session.read_text() == "untouched session"
            assert not state.guard.closed and not plugin.updating
            assert plugin.phase == ("error" if failure == "rollback" else "ready")
            assert plugin.update_error == (
                "rollback_failed" if failure == "rollback" else "update_failed"
            )
            assert len(state.launches) == 3
        finally:
            await cleanup(state)

    asyncio.run(run())


def test_busy_swap_keeps_old_service_and_retries_verified_downloads(tmp_path, monkeypatch):
    async def run():
        state = harness(tmp_path, monkeypatch)
        plugin = state.plugin
        try:
            await start_and_check(state)
            state.pause_download = True
            await plugin.update(state.guard)
            operation = plugin._operation
            await asyncio.wait_for(state.download_entered.wait(), 2)
            assert plugin.connection(DEFAULT_BRIDGE).url.startswith("http://127.0.0.1:")
            assert not state.guard.closed
            with pytest.raises(HTTPException):
                await state.guard.prepare()
            project = state.context.repository.create_project("Busy", "en", "zh", {})
            job = state.context.repository.create_job(project, "export", "synthetic")
            state.download_release.set()
            await operation
            assert plugin.phase == "ready" and plugin.update_error == "update_busy"
            assert plugin.version == "0.1.0" and len(state.launches) == 1
            assert not state.guard.closed
            requests = list(state.requests)
            state.context.repository.set_job_status(job, "done")
            await plugin.update(state.guard)
            await plugin._operation
            assert plugin.version == "0.1.1"
            assert [url for url in state.requests if "/releases/download/" in url] == [
                url for url in requests if "/releases/download/" in url
            ]
        finally:
            await cleanup(state)

    asyncio.run(run())


@pytest.mark.parametrize("phase", ["download", "health", "shutdown"])
def test_cancel_and_shutdown_release_admission_without_publishing_candidate(
    tmp_path, monkeypatch, phase
):
    async def run():
        state = harness(tmp_path, monkeypatch)
        plugin = state.plugin
        try:
            await start_and_check(state)
            before = (plugin.root / "plugin.json").read_bytes()
            state.pause_download = phase == "download"
            state.pause_health = phase != "download"
            await plugin.update(state.guard)
            entered = state.download_entered if phase == "download" else state.health_entered
            await asyncio.wait_for(entered.wait(), 2)
            assert state.guard.closed is (phase != "download")
            if phase == "shutdown":
                state.context.telemetry.runtime.accepting = False
                await plugin.stop()
                assert len(state.launches) == 2
            else:
                await plugin.cancel_update()
                assert plugin.phase == "ready"
            assert (plugin.root / "plugin.json").read_bytes() == before
            assert plugin.installation == "a" * 32 and plugin.version == "0.1.0"
            assert not state.guard.closed and not plugin.updating
            assert plugin.update_error is None
            assert not list((plugin.root / "downloads").glob("*.part"))
        finally:
            await cleanup(state)

    asyncio.run(run())


def test_swap_admission_blocks_jobs_but_allows_authenticated_cancel(tmp_path, monkeypatch):
    async def run():
        state = harness(tmp_path, monkeypatch)
        plugin = state.plugin
        try:
            await start_and_check(state)
            state.pause_health = True
            headers = {"Authorization": "Bearer test-token"}
            async with state.client(
                transport=httpx.ASGITransport(app=state.app),
                base_url="http://test",
                headers=headers,
            ) as client:
                response = await client.post("/desktop/plugins/babeldoc/update")
                assert response.status_code == 202
                await asyncio.wait_for(state.health_entered.wait(), 2)
                assert state.guard.closed
                assert (await client.post("/projects/upload")).status_code == 409
                assert (await client.post("/desktop/updates/cancel")).status_code == 409
                assert (await client.post("/desktop/plugins/babeldoc/update")).status_code == 409
                assert (
                    await client.post("/desktop/plugins/babeldoc/cancel-update")
                ).status_code == 200
                assert not state.guard.closed and plugin.phase == "ready"
        finally:
            await cleanup(state)

    asyncio.run(run())


def test_plugin_cancel_cannot_reopen_native_application_update_admission(tmp_path, monkeypatch):
    async def run():
        state = harness(tmp_path, monkeypatch)
        try:
            await start_and_check(state)
            await state.guard.prepare()
            async with state.client(
                transport=httpx.ASGITransport(app=state.app),
                base_url="http://test",
                headers={"Authorization": "Bearer test-token"},
            ) as client:
                assert (
                    await client.post("/desktop/plugins/babeldoc/cancel-update")
                ).status_code == 200
                assert state.guard.closed
                assert (await client.post("/desktop/plugins/babeldoc/update")).status_code == 409
                assert (
                    await client.post("/desktop/plugins/babeldoc/check-update")
                ).status_code == 409
            await state.guard.cancel()
        finally:
            await cleanup(state)

    asyncio.run(run())


def test_checksum_failure_does_not_stop_the_old_plugin(tmp_path, monkeypatch):
    async def run():
        state = harness(tmp_path, monkeypatch)
        plugin = state.plugin
        try:
            state.manifest["models"]["sha256"] = "0" * 64
            await start_and_check(state)
            before = (plugin.root / "plugin.json").read_bytes()
            await plugin.update(state.guard)
            await plugin._operation
            assert plugin.phase == "ready" and plugin.update_error == "update_failed"
            assert plugin.connection(DEFAULT_BRIDGE)
            assert len(state.launches) == 1 and not state.guard.closed
            assert (plugin.root / "plugin.json").read_bytes() == before
            assert not list((plugin.root / "downloads").glob("*.part"))
        finally:
            await cleanup(state)

    asyncio.run(run())


def test_cancel_during_unpack_waits_for_owned_writer_and_keeps_old_service(tmp_path, monkeypatch):
    async def run():
        state = harness(tmp_path, monkeypatch)
        plugin = state.plugin
        entered, release = threading.Event(), threading.Event()
        original = packages.unpack

        def unpack(path, destination):
            entered.set()
            assert release.wait(5)
            original(path, destination)

        monkeypatch.setattr(packages, "unpack", unpack)
        try:
            await start_and_check(state)
            await plugin.update(state.guard)
            assert await asyncio.to_thread(entered.wait, 5)
            cancel = asyncio.create_task(plugin.cancel_update())
            await asyncio.sleep(0)
            assert not cancel.done()
            cancel.cancel()
            await asyncio.sleep(0)
            assert not cancel.done()
            release.set()
            await cancel
            assert plugin.phase == "ready" and plugin.version == "0.1.0"
            assert plugin.update_error is None and not state.guard.closed
            assert not list((plugin.root / "installed").glob(".staging-*"))
            assert len(state.launches) == 1
        finally:
            release.set()
            await cleanup(state)

    asyncio.run(run())


def test_cancel_while_stopping_old_service_waits_for_process_exit(tmp_path, monkeypatch):
    async def run():
        state = harness(tmp_path, monkeypatch)
        plugin = state.plugin
        stopping, release, abandoned = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def wait():
            try:
                await release.wait()
            finally:
                if not release.is_set():
                    abandoned.set()

        try:
            await start_and_check(state)
            child = state.launches[0][2]
            child.stdin.close, child.wait = stopping.set, wait
            await plugin.update(state.guard)
            await asyncio.wait_for(stopping.wait(), 2)
            cancel = asyncio.create_task(plugin.cancel_update())
            # Give cancellation time to reach the old process's shutdown wait.
            asyncio.get_running_loop().call_later(0.02, release.set)
            await asyncio.wait_for(cancel, 2)
            assert not abandoned.is_set()
            assert plugin.phase == "ready" and plugin.version == "0.1.0"
            assert plugin.update_error is None and not state.guard.closed
            assert [identity for identity, _env, _child in state.launches] == ["a" * 32] * 2
        finally:
            release.set()
            await cleanup(state)

    asyncio.run(run())
