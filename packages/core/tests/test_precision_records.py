"""Replay records preserve historical calls while sharing plans, glossary and outputs."""

import json
from dataclasses import asdict, replace

import pytest
from wenyi_core.glossary.store import GlossaryTerm
from wenyi_core.llm.routing import identity
from wenyi_core.pipeline.precision import PrecisionBatchExecutor, _PrecisionRun
from wenyi_core.pipeline.precision_records import load_precision_call
from wenyi_core.storage.precision_archive import PrecisionArchive

from tests.fake_llm import MeteredFakeClient
from tests.precision_fixtures import PrecisionHandler as Handler
from tests.precision_fixtures import artifact as _artifact
from tests.precision_fixtures import precision_config as _config
from tests.precision_fixtures import precision_plan as _plan
from tests.precision_fixtures import precision_store as _store


def calls(store, result):
    return [
        key
        for key in store.list_artifacts(f"{result.precision_key}/calls/")
        if key.endswith(".json")
    ]


def test_historical_calls_reconstruct_messages_outputs_and_share_results(tmp_path, monkeypatch):
    store, config = _store(tmp_path), _config(tmp_path)
    plan = replace(_plan(store), terms=(GlossaryTerm(source="one", target="一", note="old note"),))
    client = MeteredFakeClient(handler=Handler())
    result = PrecisionBatchExecutor(client, config).execute(plan, store)
    assert store.read_artifact(f"{result.precision_key}/inputs.json") is None
    assert store.read_artifact(f"{result.precision_key}/ready.json") is None
    assert store.read_artifact(f"{result.precision_key}/synthesis.json") is None
    assert len(calls(store, result)) == 4
    meta = _artifact(store, f"{result.precision_key}/meta.json")
    archive = PrecisionArchive(store)
    restored = archive.thaw_plan(meta["plan"])
    assert restored == json.loads(json.dumps(asdict(plan)))
    old_glossary = meta["plan"]["glossary_ref"]
    with store.state_lock():
        archive.glossary((GlossaryTerm(source="one", target="改名", note="new note"),))
    monkeypatch.setattr(
        "wenyi_core.agents.prompts.render_glossary",
        lambda *_: pytest.fail("Replay used the current glossary renderer"),
    )
    assert archive.load_glossary(old_glossary)[0].target == "一"
    reconstructed = [load_precision_call(store, key) for key in calls(store, result)]
    actual_requests = [(call["operation"], call["messages"]) for call in client.calls]
    for record in reconstructed:
        assert (record["operation"], record["messages"]) in actual_requests
        assert record["status"] == "completed"
        assert record["json_mode"] is True
        assert record["max_tokens"] is None
        assert record["model_elapsed_ms"] >= 0
        assert record["effective_model_known"] is False
        assert record["usage_known"] is False
        assert json.loads(record["raw_response"])["translations"] == record["targets"]
    saved = _artifact(store, f"{result.precision_key}/result.json")["payload"]
    first = _artifact(store, f"{result.precision_key}/drafts/T1.json")["payload"]
    assert saved["draft_ref"] == first["targets_ref"]
    assert archive.get(saved["targets_ref"]) == list(result.targets)


def test_failed_output_keeps_response_and_retry_only_adds_one_new_call(tmp_path):
    store, config, handler = _store(tmp_path), _config(tmp_path), Handler(invalid=True)
    client = MeteredFakeClient(handler=handler)
    executor = PrecisionBatchExecutor(client, config)
    with pytest.raises(ValueError):
        executor.execute(_plan(store), store)
    key = next(
        key for key in store.list_artifacts("precision/chapters/0/") if "/calls/synthesis/" in key
    )
    failed = load_precision_call(store, key)
    assert failed["status"] == "failed"
    assert failed["error_category"] == "invalid_output"
    assert json.loads(failed["raw_response"]) == {"translations": []}
    assert "targets" not in failed
    handler.invalid = False
    result = executor.execute(_plan(store), store)
    assert len(client.calls) == 5
    assert len(calls(store, result)) == 5
    assert load_precision_call(store, key)["status"] == "failed"


def test_compatible_legacy_ready_is_upgraded_without_new_model_calls_or_deleted_data(tmp_path):
    store, config = _store(tmp_path), _config(tmp_path)
    plan = _plan(store)
    client = MeteredFakeClient(handler=Handler())
    executor = PrecisionBatchExecutor(client, config)
    result = executor.execute(plan, store)
    root = result.precision_key
    assert root is not None
    payload = {"targets": list(result.targets), "draft": list(result.before_polish)}
    store.write_artifact(
        f"{root}/ready.json",
        {
            "fingerprint": root.rsplit("/", 1)[-1],
            "stage": "ready",
            "payload_hash": identity(payload),
            "payload": payload,
        },
    )
    store.write_artifact(f"{root}/inputs.json", {"plan": asdict(plan)})
    store.delete_artifact(f"{root}/result.json")
    for key in calls(store, result):
        store.delete_artifact(key)
    later = executor.execute(plan, store)
    assert later == result
    assert len(client.calls) == 4
    assert not calls(store, later)
    assert _artifact(store, f"{root}/ready.json")["payload"] == payload
    assert store.read_artifact(f"{root}/inputs.json") is not None
    assert _artifact(store, f"{root}/result.json")["payload"]["legacy_unrecorded_request"] is True


def test_changed_call_record_fails_integrity_validation(tmp_path):
    store, config = _store(tmp_path), _config(tmp_path)
    result = PrecisionBatchExecutor(MeteredFakeClient(handler=Handler()), config).execute(
        _plan(store), store
    )
    key = calls(store, result)[0]
    record = _artifact(store, key)
    record["status"] = "fabricated"
    store.write_artifact(key, record)
    with pytest.raises(ValueError, match="changed"):
        load_precision_call(store, key)


@pytest.mark.parametrize("stage", ["drafts/T1", "result"])
def test_completed_paid_receipt_recovers_gap_before_stage_checkpoint(tmp_path, monkeypatch, stage):
    store, config = _store(tmp_path), _config(tmp_path)
    client = MeteredFakeClient(handler=Handler())
    executor = PrecisionBatchExecutor(client, config)
    original = _PrecisionRun.write
    failed = False

    def interrupt(self, name, payload):
        nonlocal failed
        if name == stage and not failed:
            failed = True
            raise RuntimeError("gap after completed model receipt")
        return original(self, name, payload)

    monkeypatch.setattr(_PrecisionRun, "write", interrupt)
    with pytest.raises(RuntimeError, match="gap"):
        executor.execute(_plan(store), store)
    result = executor.execute(_plan(store), store)
    assert len(client.calls) == 4
    assert len(calls(store, result)) == 4


def test_missing_publication_result_is_not_silently_skipped(tmp_path, monkeypatch):
    from wenyi_core.agents.precision import PrecisionError
    from wenyi_core.pipeline.orchestrator import Orchestrator

    store, config = _store(tmp_path), _config(tmp_path)
    service = Orchestrator(config, MeteredFakeClient(handler=Handler()))._translation

    def interrupt(*_):
        raise RuntimeError("after chapter commit")

    monkeypatch.setattr(service._precision, "mark_published", interrupt)
    with pytest.raises(RuntimeError):
        service.run(store, book_synopsis="synopsis")
    key = next(
        key
        for key in store.list_artifacts("precision/chapters/0/")
        if key.endswith("/publication.json")
    )
    record = _artifact(store, key)
    store.delete_artifact(record["result_ref"])
    later = MeteredFakeClient(handler=Handler())
    with pytest.raises(PrecisionError, match="missing result"):
        Orchestrator(config, later)._translation.run(store, book_synopsis="synopsis")
    assert not later.calls
    assert store.load_manifest()["chapters"][0]["status"] == "pending"
