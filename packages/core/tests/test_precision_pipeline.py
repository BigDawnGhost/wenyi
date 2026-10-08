"""Four-call precision execution, durable resume and guarded final publication."""

import json
import signal
from dataclasses import replace
from threading import Event, Lock

import pytest
from wenyi_core.agents.precision import PrecisionError
from wenyi_core.config import Config
from wenyi_core.pipeline import precision as precision_module
from wenyi_core.pipeline.orchestrator import Orchestrator
from wenyi_core.pipeline.precision import PrecisionBatchExecutor
from wenyi_core.pipeline.precision_records import load_precision_call
from wenyi_core.pipeline.translation import TranslationService
from wenyi_core.storage.precision_archive import PrecisionArchive

from tests.fake_llm import MeteredFakeClient
from tests.precision_fixtures import PrecisionHandler as Handler
from tests.precision_fixtures import artifact as _artifact
from tests.precision_fixtures import precision_config as _config
from tests.precision_fixtures import precision_plan as _plan
from tests.precision_fixtures import precision_store as _store


def test_four_calls_produce_synthesis_directly_and_keep_first_draft_for_comparison(tmp_path):
    store, config, handler = _store(tmp_path), _config(tmp_path), Handler()
    client = MeteredFakeClient(handler=handler)
    result = PrecisionBatchExecutor(client, config).execute(_plan(store), store)
    assert result.targets == ("润1:0", "润1:1")
    first = _artifact(store, f"{result.precision_key}/drafts/T1.json")["payload"]
    assert result.before_polish == tuple(PrecisionArchive(store).get(first["targets_ref"]))
    assert handler.counts == {"translate": 3, "synthesis": 1}
    assert [call["operation"] for call in client.calls] == ["translation.body"] * 3 + [
        "polish.body"
    ]
    assert all(call["max_tokens"] is None for call in client.calls)
    assert all(segment.target is None for segment in store.load_chapter(0).text_segments)
    assert _artifact(store, f"{result.precision_key}/publication.json")["status"] == "ready"


@pytest.mark.parametrize("boundary", range(1, 5))
def test_resume_each_checkpoint_reuses_completed_work(tmp_path, boundary):
    store, config, handler = _store(tmp_path), _config(tmp_path), Handler()
    client = MeteredFakeClient(handler=handler)
    writes = 0

    def interrupt():
        nonlocal writes
        writes += 1
        if writes == boundary:
            raise RuntimeError("interrupted")

    executor = PrecisionBatchExecutor(client, config)
    with pytest.raises(RuntimeError, match="interrupted"):
        executor.execute(_plan(store), store, checkpoint=interrupt)
    result = executor.execute(_plan(store), store)
    assert result.targets == ("润1:0", "润1:1")
    assert len(client.calls) == 4


def test_invalid_synthesis_is_not_published_and_drafts_are_not_regenerated(tmp_path):
    store, config, handler = _store(tmp_path), _config(tmp_path), Handler(invalid=True)
    client = MeteredFakeClient(handler=handler)
    executor = PrecisionBatchExecutor(client, config)
    with pytest.raises(PrecisionError):
        executor.execute(_plan(store), store)
    assert len(client.calls) == 4
    handler.invalid = False
    result = executor.execute(_plan(store), store)
    assert len(client.calls) == 5
    assert handler.counts["translate"] == 3
    assert result.targets == ("润2:0", "润2:1")
    assert all(segment.target is None for segment in store.load_chapter(0).text_segments)


@pytest.mark.parametrize("change", ["source", "context", "route"])
def test_incompatible_pending_checkpoints_are_not_reused(tmp_path, change):
    store, config = _store(tmp_path), _config(tmp_path)
    client = MeteredFakeClient(handler=Handler())
    executor, plan = PrecisionBatchExecutor(client, config), _plan(store)
    first = executor.execute(plan, store)
    if change == "source":
        manifest = store.load_manifest()
        manifest["source_sha256"] = "1" * 64
        store.save_manifest(manifest)
    elif change == "context":
        plan = replace(plan, context="new context")
    else:
        config.llm.models["default_strong"] = config.llm.models["default_strong"].model_copy(
            update={"model": "different-model"}
        )
    second = executor.execute(plan, store)
    assert second.precision_key != first.precision_key
    assert len(client.calls) == 8


def test_builtin_concurrency_ignores_legacy_values_and_preserves_saved_work(tmp_path, monkeypatch):
    store, config = _store(tmp_path), _config(tmp_path)
    document = config.model_dump()
    document["pipeline"]["precision_concurrency"] = 1
    config = Config.model_validate(document)
    workers = []
    original_pool = precision_module.ThreadPoolExecutor

    def pool(*, max_workers):
        workers.append(max_workers)
        return original_pool(max_workers=max_workers)

    monkeypatch.setattr(precision_module, "ThreadPoolExecutor", pool)
    client = MeteredFakeClient(handler=Handler())
    executor = PrecisionBatchExecutor(client, config)
    first = executor.execute(_plan(store), store)
    assert executor.execute(_plan(store), store) == first
    assert workers == [3]
    assert len(client.calls) == 4


def test_ctrl_c_cancels_model_work_before_joining_draft_threads(tmp_path, monkeypatch):
    store, config = _store(tmp_path), _config(tmp_path)
    started, release = Event(), Event()
    entered, lock = [], Lock()

    def blocking(messages, tier, json_mode):
        with lock:
            entered.append(True)
            if len(entered) == 3:
                started.set()
        # Bound a failing implementation's join rather than hanging the test suite.
        release.wait(timeout=5)
        return Handler()(messages, tier, json_mode)

    class CancellableClient(MeteredFakeClient):
        cancelled = False

        def cancel(self):
            self.cancelled = True
            release.set()

    client = CancellableClient(handler=blocking)
    previous = signal.getsignal(signal.SIGINT)

    def interrupt(_):
        assert started.wait(timeout=5)
        handler = signal.getsignal(signal.SIGINT)
        assert callable(handler)
        handler(signal.SIGINT, None)
        raise AssertionError("SIGINT did not interrupt the precision batch")

    monkeypatch.setattr(precision_module, "as_completed", interrupt)
    with pytest.raises(KeyboardInterrupt):
        PrecisionBatchExecutor(client, config).execute(_plan(store), store)
    assert client.cancelled
    assert release.is_set()
    assert signal.getsignal(signal.SIGINT) is previous


def test_intentional_blanks_and_numeric_only_batches(tmp_path):
    store, config = _store(tmp_path, ("OCR", "123")), _config(tmp_path)
    result = PrecisionBatchExecutor(MeteredFakeClient(handler=Handler(blank=True)), config).execute(
        _plan(store, allow_empty=True), store
    )
    assert result.targets == ("", "123")
    numeric = _store(tmp_path / "numeric", ("123", "---"))
    client = MeteredFakeClient(handler=Handler())
    assert PrecisionBatchExecutor(client, config).execute(_plan(numeric), numeric).targets == (
        "123",
        "---",
    )
    assert not client.calls


@pytest.mark.parametrize("boundary", [1, 4])
def test_protected_sources_survive_generation_archive_and_resume(tmp_path, boundary):
    source = "10\u2005\u20059\u2005\u20058"
    store, config, handler = _store(tmp_path, ("one", source)), _config(tmp_path), Handler()

    def normalize_spaces(messages, tier, json_mode):
        response = json.loads(handler(messages, tier, json_mode))
        response["translations"][1] = source.replace("\u2005", " ")
        return json.dumps(response)

    client = MeteredFakeClient(handler=normalize_spaces)
    executor, plan = PrecisionBatchExecutor(client, config), _plan(store)
    writes = 0

    def interrupt():
        nonlocal writes
        writes += 1
        if writes == boundary:
            raise RuntimeError("interrupted")

    with pytest.raises(RuntimeError, match="interrupted"):
        executor.execute(plan, store, checkpoint=interrupt)
    result = executor.execute(plan, store)
    assert result.targets == ("润1:0", source)
    assert result.before_polish[1] == source
    assert len(client.calls) == 4
    keys = [
        key
        for key in store.list_artifacts(f"{result.precision_key}/calls/")
        if key.endswith(".json")
    ]
    assert len(keys) == 4
    for key in keys:
        record = load_precision_call(store, key)
        assert record["status"] == "completed"
        assert record["targets"][1] == source
        assert json.loads(record["raw_response"])["translations"][1] == source.replace(
            "\u2005", " "
        )
    assert all(segment.target is None for segment in store.load_chapter(0).text_segments)


def test_cached_targets_restore_protected_sources(tmp_path):
    source = "10\u2005\u20059\u2005\u20058"
    store, config = _store(tmp_path, ("one", source)), _config(tmp_path)
    run = precision_module._PrecisionRun(
        MeteredFakeClient(handler=Handler()),
        config,
        _plan(store),
        store,
        checkpoint=None,
        progress=None,
    )
    assert run.targets(["译文", ""]) == ["译文", source]
    assert run.targets(["译文", "10 9 8"]) == ["译文", source]
    with pytest.raises(PrecisionError):
        run.targets(["", source])


def test_guarded_publication_keeps_manual_edits_and_recovers_marker(tmp_path, monkeypatch):
    store, config = _store(tmp_path), _config(tmp_path)
    client = MeteredFakeClient(handler=Handler())
    service = Orchestrator(config, client)._translation

    def interrupt(*_):
        raise RuntimeError("after publication")

    monkeypatch.setattr(service._precision, "mark_published", interrupt)
    with pytest.raises(RuntimeError, match="after publication"):
        service.run(store, book_synopsis="synopsis")
    key = next(
        key
        for key in store.list_artifacts("precision/chapters/0/")
        if key.endswith("/publication.json")
    )
    assert _artifact(store, key)["status"] == "ready"
    later = MeteredFakeClient(handler=Handler())
    Orchestrator(config, later)._translation.run(store, book_synopsis="synopsis")
    assert _artifact(store, key)["status"] == "published"
    assert not [
        call for call in later.calls if call["operation"] in {"translation.body", "polish.body"}
    ]
    usage = store.load_usage()
    assert usage is not None
    assert usage["by_stage"]["translation.body"]["calls"] == 3
    assert usage["by_stage"]["polish.body"]["calls"] == 1


def test_publication_refuses_manual_target_changes(tmp_path):
    store, config = _store(tmp_path), _config(tmp_path)
    plan = _plan(store)
    result = PrecisionBatchExecutor(MeteredFakeClient(handler=Handler()), config).execute(
        plan, store
    )
    chapter = store.load_chapter(0)
    chapter.segments[0].target = "人工编辑"
    store.save_chapter(chapter)
    with pytest.raises(PrecisionError, match="edited"):
        TranslationService.save_precision_batch(store, plan, result)
    assert store.load_chapter(0).segments[0].target == "人工编辑"


def test_cli_exports_and_resumes_with_four_body_calls(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from wenyi_cli.cli import app

    source, output = tmp_path / "sample.txt", tmp_path / "translated.txt"
    source.write_text("one\n\ntwo\n", encoding="utf-8")
    config, handler = _config(tmp_path / "state"), Handler()
    client = MeteredFakeClient(handler=handler)
    monkeypatch.setattr("wenyi_cli.commands.context.CommandContext.load_config", lambda *_: config)
    monkeypatch.setattr("wenyi_core.pipeline.runtime.build_client", lambda *_: client)
    arguments = [
        "translate",
        str(source),
        "--translation-mode",
        "best_of_three",
        "--polish",
        "--no-review",
        "--format",
        "txt",
        "--out",
        str(output),
    ]
    for _ in range(2):
        result = CliRunner().invoke(app, arguments)
        assert result.exit_code == 0, result.output
    assert "润" in output.read_text(encoding="utf-8")
    assert handler.counts == {"translate": 3, "synthesis": 1}
