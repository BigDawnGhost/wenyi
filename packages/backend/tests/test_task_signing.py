"""Queue task entries accept only payloads signed with the shared secret."""

from __future__ import annotations

import asyncio

import pytest
from wenyi_backend.workers import tasks
from wenyi_backend.workers.signing import SIGNATURE_FIELD, payload_from, sign_task, verify_task


def test_signature_binds_name_payload_and_secret():
    payload = {"project_id": "p", "run_id": "r"}
    signature = sign_task("run_parse", payload, "secret")
    assert signature == sign_task("run_parse", {"run_id": "r", "project_id": "p"}, "secret")
    assert signature != sign_task("run_export", payload, "secret")
    assert signature != sign_task("run_parse", {**payload, "run_id": "other"}, "secret")
    assert signature != sign_task("run_parse", payload, "other-secret")


def test_payload_from_strips_queue_reserved_fields():
    assert payload_from({"_job_id": "r", SIGNATURE_FIELD: "x", "project_id": "p"}) == {
        "project_id": "p"
    }


def test_verify_rejects_missing_or_foreign_signatures():
    signature = sign_task("run_parse", {"project_id": "p"}, "secret-a")
    assert verify_task("run_parse", {"project_id": "p"}, signature, "secret-a")
    assert not verify_task("run_parse", {"project_id": "p"}, signature, "secret-b")
    assert not verify_task("run_parse", {"project_id": "p"}, None, "secret-a")
    assert not verify_task("run_parse", {"project_id": "p"}, 123, "secret-a")


def test_worker_entry_rejects_unsigned_and_tampered_payloads(backend_context, monkeypatch):
    executed = []

    async def fake_run(kind, project_id, run_id, params):
        executed.append((kind, project_id, run_id, params))

    monkeypatch.setattr(tasks, "_run", fake_run)
    worker_ctx = {"backend": backend_context}
    with pytest.raises(ValueError, match="Rejecting unsigned"):
        asyncio.run(tasks.run_parse(worker_ctx, project_id="p"))
    signed = {"project_id": "p"}
    signed[SIGNATURE_FIELD] = sign_task("run_parse", signed, backend_context.api_token)
    tampered = {**signed, "project_id": "injected"}
    with pytest.raises(ValueError, match="Rejecting unsigned"):
        asyncio.run(tasks.run_parse(worker_ctx, **tampered))
    assert executed == []
    asyncio.run(tasks.run_parse(worker_ctx, **signed))
    assert executed == [("parse", "p", None, {})]
