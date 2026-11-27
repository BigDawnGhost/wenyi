"""Private writer process; the Desktop engine retains the catalog and telemetry controls."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import asdict, replace
from functools import partial
from typing import TextIO, cast

from wenyi_backend.context import BackendContext, use_context
from wenyi_backend.execution import process, runner
from wenyi_backend.execution.bindings import workflow_dependencies
from wenyi_core.pdf_bridge.connection import BridgeConnection, use_bridge_connection

from .babeldoc_plugin import DEFAULT_BRIDGE
from .diagnostics import Reporter, scope
from .local_backend import LocalBackend
from .local_credentials import CredentialStore, system_keyring
from .main import DesktopServices, assemble_context
from .settings import DesktopSettings


class _PipeServices(DesktopServices):
    def __init__(self, backend, send):
        super().__init__(backend)
        self.send = send

    def connect(self):
        return self

    def get(self, key):
        return None

    def set(self, key, value, *, ex):
        self.send({"cache": [key, value, ex]})

    def publish(self, channel, value):
        self.send({"publish": [channel, value]})


class _DiagnosticStream:
    def __init__(self, send):
        self.send = send

    def write(self, value):
        _, _, frame = value.partition(" ")
        self.send({"diagnostic": json.loads(frame)})

    def flush(self):
        pass


def execute(request, options, send):
    backend = LocalBackend(
        options["workspace"],
        options["config_path"],
        library_root=options["library_root"],
        install_root=options["install_root"],
        initialize=False,
    )
    credentials = CredentialStore(
        backend.workspace, vault_factory=system_keyring, session=options["credentials"]
    )
    services = _PipeServices(backend, send)
    context = assemble_context(backend, credentials, services)
    bridge = options["bridge"]

    def bridge_connection(url):
        if bridge is not None and url.rstrip("/") == DEFAULT_BRIDGE:
            return BridgeConnection(**bridge)
        return services.babeldoc.connection(url)

    salt = options["diagnostic_salt"]
    reporter = None
    if salt is not None:
        reporter = Reporter(cast(TextIO, _DiagnosticStream(send)))
        reporter.salt = bytes.fromhex(salt)
    try:
        with (
            use_context(context),
            use_bridge_connection(bridge_connection),
            scope(reporter, project_ref=request.project_id, run_ref=request.run_id),
        ):
            return runner.execute(request, workflow_dependencies(context), preclaimed=True)
    finally:
        if reporter is not None:
            reporter.close()
        backend.close()


def workflow_supervisor(context: BackendContext, diagnostics: Reporter | None):
    backend, services, settings = context.repository, context.telemetry, context.settings_store
    assert isinstance(backend, LocalBackend)
    assert isinstance(services, DesktopServices)
    assert isinstance(settings, DesktopSettings)
    # The supervisor may append a stop event after reaping its writer, but
    # must never create a missing project or database to do so.
    dependencies = replace(
        workflow_dependencies(context), storage_for=partial(backend.storage_for, create=False)
    )

    async def run(request):
        command = [sys.executable]
        env = None
        if not getattr(sys, "frozen", False):
            command += ["-m", "wenyi_desktop"]
            if os.name == "nt" and sys.prefix != sys.base_prefix:
                # Like the native host, own CPython itself rather than the venv redirector.
                command[0] = sys._base_executable
                env = {**os.environ, "__PYVENV_LAUNCHER__": sys.executable}
        command += ["--workflow-child"]
        options = {
            "workspace": str(backend.workspace),
            "config_path": backend.config_path,
            "library_root": str(backend.library_root) if backend.library_root else None,
            "install_root": str(backend.install_root) if backend.install_root else None,
            "credentials": settings.credentials.session_snapshot(),
            "bridge": _bridge_snapshot(services),
            "diagnostic_salt": diagnostics.salt.hex() if diagnostics is not None else None,
        }

        def receive(frame):
            hub = services.connect()
            if "cache" in frame:
                key, value, expiry = frame["cache"]
                hub.set(key, value, ex=expiry)
            elif "publish" in frame:
                hub.publish(*frame["publish"])
            elif "diagnostic" in frame and diagnostics is not None:
                row = frame["diagnostic"]
                diagnostics.emit(row["event"], **row["attributes"])

        assert services.runtime is not None and request.run_id is not None
        with services.runtime.watch_pause(request.run_id) as pause_requested:
            await process.run(
                request,
                dependencies,
                command,
                options,
                receive=receive,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
                pause_requested=pause_requested,
            )

    return run


def _bridge_snapshot(services: DesktopServices) -> dict | None:
    from wenyi_core.pdf_bridge.client import BabeldocBridgeError

    try:
        return asdict(services.babeldoc.connection(DEFAULT_BRIDGE))
    except BabeldocBridgeError:
        return None
