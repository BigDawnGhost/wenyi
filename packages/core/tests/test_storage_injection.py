"""Storage injection must keep review and subtitle state out of resource directories."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from wenyi_core.config import Config
from wenyi_core.ingest.segmenter import load_document
from wenyi_core.pipeline.input_preparation import ingest_config, load_parsed_document
from wenyi_core.pipeline.orchestrator import Orchestrator
from wenyi_core.pipeline.runstore import source_sha256
from wenyi_core.review.run_store import ReviewRunStore
from wenyi_core.srt.store import SrtRunStore
from wenyi_core.srt.translate import translate_srt
from wenyi_core.storage.file import FileStorage
from wenyi_core.storage.protocol import Storage
from wenyi_core.storage.sqlite import SqliteStorage

from tests.fake_llm import MeteredFakeClient, routing_handler
from tests.storage_fixtures import MemoryArtifacts


@pytest.mark.parametrize("storage_type", [FileStorage, SqliteStorage])
def test_full_workflow_preserves_review_and_legacy_reports_on_resume(tmp_path, storage_type):
    source = tmp_path / "book.txt"
    source.write_text("First chapter\n\nA short story.\n", encoding="utf-8")
    config = Config.from_dict(
        {
            "language": {"source": "en", "target": "zh"},
            "llm": {"preset": "fake"},
            "pipeline": {"book_understanding": False, "review": True, "review_autofix": False},
        }
    )
    store = storage_type(str(tmp_path / "state"))
    client = MeteredFakeClient(handler=routing_handler)
    output = str(tmp_path / "translated.txt")
    try:
        first = Orchestrator(config, client=client, storage=store).run_all(
            str(source), out_format="txt", out_path=output
        )
        assert store.read_artifact("report.json") is None
        assert "report" not in first
        review = store.load_latest_review_result()
        assert review is not None
        usage = store.load_usage()
        assert usage is not None and usage["totals"]["calls"] > 0
        legacy = {"summary": {"chapters_done": 1}, "legacy": True}
        store.write_artifact("report.json", legacy)

        resumed_client = MeteredFakeClient(handler=routing_handler)
        resumed = Orchestrator(config, client=resumed_client, storage=store).run_all(
            str(source), out_format="txt", out_path=output
        )

        assert store.read_artifact("report.json") == legacy
        assert store.pending_chapters() == []
        assert resumed["review_result"]["review_id"] == review["review_id"]
        assert resumed_client.calls == []
        assert store.load_usage() == usage
        assert Path(output).is_file()
        assert all(event["event"] != "report_saved" for event in store.list_events(limit=0))
    finally:
        store.close()


def test_review_rounds_resume_and_events_use_artifacts_without_creating_files(tmp_path):
    resource_dir = tmp_path / "resources"
    store = MemoryArtifacts(resource_dir)
    review = ReviewRunStore(store.run_dir, storage=store)
    review.start(reviewed_content_digest="digest", metadata={"config": {"rounds": 2}})
    with review.round_scope(1):
        review.write_json("evidence/ch0.json", {"matches": ["a"]})
        review.log_event("evidence_read")
    review.mark_chunk_done("r1-ch0-base0-n1", {"initial_issues": [{"index": 0}]})
    review.save_checkpoint({"round": 1})
    review.mark_interrupted()
    restored = ReviewRunStore.find_resumable(
        store.run_dir, "digest", config={"rounds": 2}, storage=store
    )
    assert restored is not None
    assert restored.run_dir == review.run_dir
    assert restored.load_checkpoint() == {"round": 1}
    assert restored.load_chunk_result("r1-ch0-base0-n1") == {"initial_issues": [{"index": 0}]}
    restored.log_event("resume")
    events = store.read_artifact_records(f"reviews/{review.review_id}/events.jsonl")
    assert [row["seq"] for row in events] == list(range(1, len(events) + 1))
    assert ReviewRunStore.find_resumable(store.run_dir, "other", storage=store) is None
    assert not resource_dir.exists()


def test_preparation_reuses_matching_preview_and_invalidates_changed_settings(
    tmp_path, monkeypatch
):
    source = tmp_path / "book.txt"
    source.write_text("First chapter\n\nA short story.\n", encoding="utf-8")
    config = Config.from_dict(
        {"language": {"source": "en", "target": "zh"}, "llm": {"preset": "fake"}}
    )
    store = FileStorage(str(tmp_path / "state"))
    assert isinstance(store, Storage)
    document = load_document(
        str(source), "en", "zh", split_segments=config.segment.max_tokens_per_segment
    )
    store.write_artifact(
        "parsed_document.json",
        {
            "source_sha256": source_sha256(str(source)),
            "ingest_config": ingest_config(config),
            "document": document.model_dump(mode="json"),
        },
    )
    changed = config.model_copy(deep=True)
    changed.segment.max_tokens_per_segment += 1
    assert load_parsed_document(store, str(source), changed) is None

    def unexpected_parse(*args, **kwargs):
        raise AssertionError("Preparation re-parsed a matching upload preview")

    monkeypatch.setattr("wenyi_core.pipeline.input_preparation.load_document", unexpected_parse)
    result = Orchestrator(
        config, client=MeteredFakeClient(handler=routing_handler), storage=store
    ).prepare(str(source))
    assert result is store
    assert store.load_manifest()["initialized"] is True
    assert store.load_manifest()["source_sha256"] == source_sha256(str(source))
    store.close()


def test_subtitle_pause_flushes_usage_and_resume_preserves_human_edits(tmp_path):
    source = tmp_path / "captions.srt"
    source.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello.\n\n", encoding="utf-8")
    config = Config.from_dict(
        {
            "language": {"source": "en", "target": "zh"},
            "llm": {"preset": "fake"},
            "output": {"mono": True, "bilingual": True},
        }
    )
    storage = MemoryArtifacts(tmp_path / "resources")
    client = MeteredFakeClient(handler=lambda *args: json.dumps({"1": "你好。"}))

    def pause(done, total, label):
        if done:
            raise RuntimeError("pause requested")

    with pytest.raises(RuntimeError, match="pause requested"):
        translate_srt(str(source), config, client=client, storage=storage, progress=pause)
    usage = storage.load_usage()
    assert usage is not None
    assert usage["totals"]["calls"] == 1
    manifest = storage.read_artifact("srt/manifest.json")
    assert isinstance(manifest, dict)
    assert manifest["status"] == "interrupted"
    subtitle = SrtRunStore(storage.run_dir, storage=storage)
    cues = subtitle.load_cues()
    cues["1"]["target"] = "人工校订。"
    subtitle.save_cues(cues)
    resumed = MeteredFakeClient(
        handler=lambda *args: pytest.fail("Completed cue was retransmitted")
    )
    result = translate_srt(str(source), config, client=resumed, storage=storage)
    assert not resumed.calls
    assert result["usage"]["totals"]["calls"] == 1
    assert storage.manifest["initialized"]
    assert subtitle.load_cues()["1"]["target"] == "人工校订。"
    for output in result["outputs"]:
        text = Path(output).read_text(encoding="utf-8")
        assert "人工校订。" in text
        assert "00:00:01,000 --> 00:00:02,000" in text
    assert not Path(storage.run_dir).exists()


def test_domain_services_have_no_direct_state_files_or_sqlite():
    core = Path(__file__).resolve().parents[1] / "wenyi_core"
    for path in (core / "pipeline").glob("*.py"):
        if path.name == "runstore.py":
            continue  # The explicit CLI filesystem backend.
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                assert not (
                    isinstance(node.func, ast.Name) and node.func.id in {"open", "GlossaryStore"}
                ), path
                assert not (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"read_text", "write_text", "listdir"}
                ), path
    for path in core.rglob("*.py"):
        imports = [
            node
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, (ast.Import, ast.ImportFrom))
        ]
        for node in imports:
            modules = (
                [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else [alias.name for alias in node.names]
            )
            assert not any(
                module.split(".")[0] in {"wenyi_cli", "typer", "rich"} for module in modules
            ), path
