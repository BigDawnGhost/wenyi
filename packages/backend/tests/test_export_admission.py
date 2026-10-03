"""Admission failures are reported without trying to roll back nonexistent job identities."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from wenyi_backend.routers import export
from wenyi_backend.schemas import ExportRequest
from wenyi_core.config import Config


def test_export_admission_failure_is_service_unavailable(backend_context, monkeypatch):
    monkeypatch.setattr(
        export,
        "require_project",
        lambda pid: {"id": pid, "fmt": "text", "initialized": True},
    )
    monkeypatch.setattr(export, "effective_config", lambda project: Config())
    monkeypatch.setattr(
        export,
        "storage_for",
        lambda pid: SimpleNamespace(load_manifest=lambda: {}, close=lambda: None),
    )
    backend_context.exports.admit_export.side_effect = OSError("offline")
    with pytest.raises(HTTPException) as error:
        asyncio.run(export.enqueue_export("project", ExportRequest()))
    assert error.value.status_code == 503
    backend_context.exports.fail_export_admission.assert_not_called()
