"""App-owned, opt-in BabelDOC process and installation lifecycle (HTTP only)."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from fastapi import HTTPException
from wenyi_core.pdf_bridge.client import BabeldocBridgeError
from wenyi_core.pdf_bridge.connection import BridgeConnection

from . import plugin_packages as packages

if TYPE_CHECKING:
    from .updates import UpdateGuard

DEFAULT_BRIDGE = "http://127.0.0.1:8765"
ACTIVE_PHASES = {"checking", "runtime", "models", "unpacking", "starting", "stopping"}


def child_environment(token: str) -> dict[str, str]:
    """Inherit OS necessities, not provider credentials or frozen-host loader state."""
    allowed = {
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "SYSTEMDRIVE",
        "COMSPEC",
        "TMP",
        "TEMP",
        "TMPDIR",
        "HOME",
        "USERPROFILE",
        "LOCALAPPDATA",
        "APPDATA",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
    }
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env["WENYI_BABELDOC_TOKEN"] = token
    return env


async def drain_owned(task: asyncio.Task):
    """Repeated shutdown/request cancellation must not abandon an owned operation."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


async def finish_thread(function, *args):
    """Never abandon a writer when cancellation races with extraction/publication."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await drain_owned(task)
        raise


class BabeldocPlugin:
    def __init__(self, workspace: Path):
        self.root = workspace / "plugins" / "babeldoc"
        self.target = packages.platform_key()
        self.enabled = False
        self.installation: str | None = None
        self.version: str | None = None
        self.phase = "disabled"
        self.error: str | None = None
        self.downloaded = 0
        self.total: int | None = None
        self.latest_version: str | None = None
        self.checking_update = False
        self.updating = False
        self.update_error: str | None = None
        self._manifest: dict | None = None
        self._connection: BridgeConnection | None = None
        self._child: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._operation: asyncio.Task | None = None
        self._closing = False
        self.control = asyncio.Lock()
        self._load()

    def _load(self) -> None:
        try:
            saved = json.loads((self.root / "plugin.json").read_text(encoding="utf-8"))
            self.enabled = saved["enabled"] is True
            directory = saved.get("installation")
            # Only a completed direct child of our installation directory can be executed.
            if (
                directory
                and len(directory) == 32
                and all(c in "0123456789abcdef" for c in directory)
            ):
                path = self.root / "installed" / directory
                if path.is_dir() and not path.is_symlink():
                    self.installation = directory
                    self.version = saved.get("version")
        except FileNotFoundError:
            return
        except (OSError, ValueError, KeyError, TypeError):
            self.error = "state_failed"
            self.phase = "error"

    def _save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.root / "plugin.json.tmp"
        temporary.write_text(
            json.dumps(
                {
                    "enabled": self.enabled,
                    "installation": self.installation,
                    "version": self.version,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.root / "plugin.json")

    @property
    def busy(self) -> bool:
        return self.phase in ACTIVE_PHASES or self.checking_update or self.updating

    @property
    def update_available(self) -> bool:
        return bool(
            self.version
            and self.latest_version
            and packages.version_key(self.latest_version) > packages.version_key(self.version)
        )

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "installed": self.installation is not None,
            "supported": self.target is not None,
            "phase": self.phase,
            "version": self.version,
            "downloaded": self.downloaded,
            "total": self.total,
            "error": self.error,
            "latest_version": self.latest_version,
            "update_available": self.update_available,
            "checking_update": self.checking_update,
            "updating": self.updating,
            "update_error": self.update_error,
        }

    def connection(self, url: str) -> BridgeConnection:
        # Custom HTTP bridges remain usable. The default endpoint is managed on Desktop.
        if url.rstrip("/") != DEFAULT_BRIDGE:
            return BridgeConnection(url)
        connection = self._connection
        if not self.enabled or connection is None:
            raise BabeldocBridgeError(
                "Enable the BabelDOC plugin in Desktop Settings and wait until it is ready."
            )
        return connection

    def restore(self) -> None:
        if self.enabled:
            if self.installation:
                self._closing = False
                self._task = asyncio.create_task(self._run(install=False))
                self.phase = "starting"
            else:
                self.phase, self.error = "error", "interrupted"

    async def enable(self) -> None:
        async with self.control:
            if not self.target:
                raise ValueError("unsupported")
            if self.busy or self.phase == "ready":
                return
            await self.stop()
            self._closing = False
            self.enabled = True
            self._save()
            self.phase = "starting" if self.installation else "checking"
            self.error = None
            self._task = asyncio.create_task(self._run(install=self.installation is None))

    async def disable(self) -> None:
        async with self.control:
            self.enabled = False
            self._save()
            await self.stop()
            self.phase, self.error = "disabled", None
            self.downloaded, self.total = 0, None

    async def check_update(self) -> None:
        async with self.control:
            if not self.target or not self.installation or self.busy:
                raise ValueError("Plugin update check is unavailable")
            self.checking_update = True
            self.latest_version, self._manifest, self.update_error = None, None, None
            self._operation = asyncio.create_task(self._check_update())

    async def _check_update(self) -> None:
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=60,
                event_hooks={"request": [packages.require_https]},
            ) as client:
                manifest = await packages.read_manifest(client, self.target)
            if self.version:
                packages.version_key(self.version)
            self.latest_version, self._manifest = manifest["version"], manifest
        except asyncio.CancelledError:
            raise
        except Exception:
            self.update_error = "check_failed"
        finally:
            self.checking_update = False

    async def update(self, guard: UpdateGuard) -> None:
        async with self.control:
            if (
                not self.target
                or not self.installation
                or self.busy
                or guard.closed
                or not self._manifest
                or not self.update_available
            ):
                raise ValueError("Plugin update is unavailable")
            self._closing = False
            self.updating = True
            self.phase, self.update_error = "checking", None
            self.downloaded, self.total = 0, None
            self._operation = asyncio.create_task(self._update(self._manifest, guard))

    async def cancel_update(self) -> None:
        async with self.control:
            if self.updating:
                await self._cancel_operation()

    async def _update(self, manifest: dict, guard: UpdateGuard) -> None:
        previous = self.installation
        switched = committed = closed = False
        failure = None
        try:
            identity, version = await self._install(manifest)
            # Downloads do not interrupt the old service. Only the final swap
            # closes job admission, checks every job, and stops the old process.
            await guard.prepare(allow_plugin_activity=True)
            closed = True
            switched = True
            await self._stop_service()
            await self._start(identity)
            if not self.enabled:
                await self._stop_child()
            self._select_installation(identity, version)
            committed = True
            self.phase, self.error = "ready" if self.enabled else "disabled", None
            if self.enabled:
                self._task = asyncio.create_task(self._watch())
        except asyncio.CancelledError:
            raise
        except HTTPException:
            failure = "update_busy"
        except Exception:
            failure = "update_failed"
        finally:

            async def settle() -> None:
                nonlocal failure
                try:
                    if not committed:
                        if switched:
                            await self._stop_service()
                            if self.enabled and not self._closing and previous is not None:
                                try:
                                    await self._start(previous)
                                    self._task = asyncio.create_task(self._watch())
                                except Exception:
                                    await self._stop_child()
                                    failure = "rollback_failed"
                        if self._connection is not None:
                            self.phase, self.error = "ready", None
                        elif not self.enabled or self._closing:
                            self.phase = "disabled"
                        else:
                            self.phase, self.error = "error", "start_failed"
                finally:
                    try:
                        if closed:
                            await guard.cancel(allow_plugin_activity=True)
                    finally:
                        self.updating = False
                        self.update_error = failure
                        self.downloaded, self.total = 0, None

            # A cancelled request, another cancel, or app shutdown must not leave
            # a half-stopped service or an admission gate owned by a dead task.
            await drain_owned(asyncio.create_task(settle()))

    async def _install(self, manifest: dict | None = None) -> tuple[str, str]:
        self.phase = "checking"
        self.root.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=60,
            event_hooks={"request": [packages.require_https]},
        ) as client:
            if manifest is None:
                manifest = await packages.read_manifest(client, self.target)
            version = manifest["version"]
            archives = self.root / "downloads"
            archives.mkdir(exist_ok=True)
            for kind in ("runtime", "models"):
                self.phase = kind
                await packages.download(
                    client,
                    version,
                    manifest[kind],
                    archives / f"{manifest[kind]['sha256']}.zip",
                    self._progress,
                )
        self.phase = "unpacking"
        self.downloaded, self.total = 0, None
        installed = self.root / "installed"
        installed.mkdir(exist_ok=True)
        # Each staging directory is owned by this operation. Completed versions and
        # persistent sessions are never replaced or removed by a failed installation.
        with tempfile.TemporaryDirectory(prefix=".staging-", dir=installed) as staging:
            stage = Path(staging)
            for kind, subdir in (("runtime", "runtime"), ("models", "cache")):
                await finish_thread(
                    packages.unpack,
                    archives / f"{manifest[kind]['sha256']}.zip",
                    stage / subdir,
                )
            executable = stage / "runtime" / self.executable_name
            if not executable.is_file():
                raise ValueError("Plugin executable is missing")
            identity = secrets.token_hex(16)
            destination = installed / identity
            # TemporaryDirectory owns the now-empty staging path, not the published tree.
            stage.rename(destination)
            return identity, version

    def _select_installation(self, identity: str, version: str) -> None:
        previous = self.installation, self.version
        self.installation, self.version = identity, version
        try:
            self._save()
        except OSError:
            self.installation, self.version = previous
            raise

    @property
    def executable_name(self) -> str:
        return "wenyi-babeldoc-plugin.exe" if sys.platform == "win32" else "wenyi-babeldoc-plugin"

    def _progress(self, downloaded: int, total: int) -> None:
        self.downloaded, self.total = downloaded, total

    async def _spawn(self, command: list[str], env: dict[str, str]) -> None:
        task = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *command,
                cwd=self.root,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                **(
                    {"creationflags": subprocess.CREATE_NO_WINDOW}
                    if sys.platform == "win32"
                    else {}
                ),
            )
        )
        try:
            self._child = await asyncio.shield(task)
        except asyncio.CancelledError:
            self._child = await drain_owned(task)
            raise

    async def _run(self, *, install: bool) -> None:
        try:
            if install:
                identity, version = await self._install()
            else:
                identity, version = self.installation, self.version
            if identity is None:
                raise ValueError("Plugin installation is missing")
            await self._start(identity)
            if install:
                assert version is not None
                self._select_installation(identity, version)
            self.phase, self.error = "ready", None
        except asyncio.CancelledError:
            await self._stop_child()
            raise
        except Exception:
            # Do not echo arbitrary HTTP bodies, process output, or credentials.
            self.error = (
                "install_failed"
                if self.phase in {"checking", "runtime", "models", "unpacking"}
                else "start_failed"
            )
            self.phase = "error"
            await self._stop_child()
            return
        await self._watch()

    async def _start(self, identity: str) -> None:
        self.phase = "starting"
        self.downloaded, self.total = 0, None
        directory = self.root / "installed" / identity
        token = secrets.token_hex(32)
        await self._spawn(
            [
                str(directory / "runtime" / self.executable_name),
                "--data-dir",
                str(self.root / "sessions"),
                "--cache-dir",
                str(directory / "cache"),
            ],
            child_environment(token),
        )
        child = self._child
        line = await asyncio.wait_for(child.stdout.readline(), timeout=180)
        ready = json.loads(line)
        if (
            ready.get("protocol") != 1
            or ready.get("pid") != child.pid
            or type(ready.get("port")) is not int
            or not 0 < ready["port"] < 65536
        ):
            raise ValueError("Invalid plugin identity")
        connection = BridgeConnection(
            f"http://127.0.0.1:{ready['port']}",
            {"Authorization": f"Bearer {token}"},
            trust_env=False,
        )
        async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
            response = await client.get(f"{connection.url}/health", headers=connection.headers)
            response.raise_for_status()
            if response.json().get("ok") is not True:
                raise ValueError("Plugin health check failed")
        self._connection = connection

    async def _watch(self) -> None:
        try:
            await self._child.wait()
            raise RuntimeError("Plugin exited unexpectedly")
        except asyncio.CancelledError:
            raise
        except Exception:
            if not self.updating or self.phase == "ready":
                self.phase, self.error = "error", "start_failed"
        finally:
            await self._stop_child()

    async def _stop_child(self) -> None:
        self._connection = None
        child, self._child = self._child, None
        if child is None:
            return

        async def close() -> None:
            if child.stdin:
                child.stdin.close()
            try:
                await asyncio.wait_for(child.wait(), timeout=10)
            except asyncio.TimeoutError:
                child.kill()
                await child.wait()

        # Keep ownership until exit even if cancelling an upgrade interrupts
        # the watcher while it is already stopping the previous runtime.
        task = asyncio.create_task(close())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await drain_owned(task)
            raise

    async def _cancel_operation(self) -> None:
        operation, self._operation = self._operation, None
        if operation is not None:
            operation.cancel()
            try:
                await drain_owned(operation)
            except asyncio.CancelledError:
                pass

    async def _stop_service(self) -> None:
        self._connection = None
        task, self._task = self._task, None
        if task is not None:
            self.phase = "stopping"
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._stop_child()

    async def stop(self) -> None:
        self._closing = True
        await self._cancel_operation()
        await self._stop_service()
