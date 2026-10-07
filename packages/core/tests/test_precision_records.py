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


@pytest.mark.parametrize("boundary", ["drafts", "ready"])
def test_old_reading_policy_checkpoints_are_preserved_but_not_continued(
    tmp_path, monkeypatch, boundary
):
    from wenyi_core.agents import prompts

    store, config = _store(tmp_path), _config(tmp_path)
    term = GlossaryTerm("one", "一", reading="OLD_READING")
    plan = replace(_plan(store), terms=(term,))
    old_client = MeteredFakeClient(handler=Handler())
    old = _PrecisionRun(old_client, config, plan, store, checkpoint=None, progress=None)
    # Recreate the pre-filter identity and renderer without altering any stored raw term.
    old.fingerprint = identity(
        {key: value for key, value in old.binding.items() if key != "glossary_reading_source"}
    )
    old.key = f"precision/chapters/0/0-2/{old.fingerprint}"
    render = prompts.render_glossary
    write = old.write

    def stop_after_draft(name, payload):
        write(name, payload)
        if name.startswith("drafts/"):
            raise RuntimeError("old draft interrupted")

    if boundary == "drafts":
        monkeypatch.setattr(old, "write", stop_after_draft)
    with monkeypatch.context() as context:
        context.setattr(
            prompts,
            "render_glossary",
            lambda terms, **_: render(terms, source_lang="ja"),
        )
        if boundary == "drafts":
            with pytest.raises(RuntimeError, match="old draft"):
                old.execute()
        else:
            old.execute()
    meta_key = f"{old.key}/meta.json"
    metadata = _artifact(store, meta_key)
    metadata.pop("glossary_reading_source", None)
    store.write_artifact(meta_key, metadata)
    old_artifacts = {key: store.read_artifact(key) for key in store.list_artifacts(old.key + "/")}
    old_calls = [key for key in old_artifacts if "/calls/" in key and key.endswith(".json")]
    assert old_calls
    historical = [load_precision_call(store, key) for key in old_calls]
    assert all("OLD_READING" in json.dumps(call["messages"]) for call in historical)

    new_client = MeteredFakeClient(handler=Handler())
    result = PrecisionBatchExecutor(new_client, config).execute(plan, store)
    assert result.precision_key != old.key
    assert len(new_client.calls) == 4
    metadata = _artifact(store, f"{result.precision_key}/meta.json")
    assert metadata["glossary_reading_source"] == "ja"
    assert all("OLD_READING" not in json.dumps(call["messages"]) for call in new_client.calls)
    assert all(store.read_artifact(key) == value for key, value in old_artifacts.items())
    assert [load_precision_call(store, key) for key in old_calls] == historical
    assert PrecisionArchive(store).load_glossary(
        _artifact(store, meta_key)["plan"]["glossary_ref"]
    ) == (term,)
    assert all(segment.target is None for segment in store.load_chapter(0).text_segments)


def test_published_chapter_is_not_retranslated_after_reading_policy_change(tmp_path, monkeypatch):
    from wenyi_core.agents import prompts
    from wenyi_core.pipeline import precision
    from wenyi_core.pipeline.orchestrator import Orchestrator

    store, config = _store(tmp_path), _config(tmp_path)
    store.upsert_term(GlossaryTerm("one", "一", reading="OLD_READING"))
    render = prompts.render_glossary

    def old_identity(value):
        if isinstance(value, dict):
            value = {key: item for key, item in value.items() if key != "glossary_reading_source"}
        return identity(value)

    with monkeypatch.context() as context:
        context.setattr(precision, "identity", old_identity)
        context.setattr(
            prompts, "render_glossary", lambda terms, **_: render(terms, source_lang="ja")
        )
        Orchestrator(config, MeteredFakeClient(handler=Handler()))._translation.run(
            store, book_synopsis="synopsis"
        )
    assert store.load_manifest()["chapters"][0]["status"] == "done"
    chapter = store.load_chapter(0)
    old_keys = store.list_artifacts("precision/chapters/0/")
    for key in old_keys:
        if key.endswith("/meta.json"):
            metadata = _artifact(store, key)
            metadata.pop("glossary_reading_source", None)
            store.write_artifact(key, metadata)
    artifacts = {key: store.read_artifact(key) for key in old_keys}
    client = MeteredFakeClient(handler=Handler())
    Orchestrator(config, client)._translation.run(store, book_synopsis="synopsis")
    assert not [
        call for call in client.calls if call["operation"] in {"translation.body", "polish.body"}
    ]
    assert store.load_chapter(0) == chapter
    assert store.list_artifacts("precision/chapters/0/") == old_keys
    assert all(store.read_artifact(key) == value for key, value in artifacts.items())
