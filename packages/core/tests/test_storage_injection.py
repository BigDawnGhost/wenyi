"""Storage injection must keep review and subtitle state out of resource directories."""

from __future__ import annotations

import ast
import json
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path

import pytest
from wenyi_core.config import Config
from wenyi_core.ingest.segmenter import load_document
from wenyi_core.pipeline.orchestrator import Orchestrator
from wenyi_core.pipeline.preparation import PreparationService
from wenyi_core.pipeline.runstore import source_sha256
from wenyi_core.review.run_store import ReviewRunStore
from wenyi_core.srt.store import SrtRunStore
from wenyi_core.srt.translate import translate_srt
from wenyi_core.storage.file import FileStorage
from wenyi_core.storage.protocol import Storage

from tests.fake_llm import MeteredFakeClient, routing_handler


def require_file_storage(store: Storage) -> FileStorage:
    """CLI/offline tests use the file backend; narrow Storage to FileStorage for path asserts."""
    if not isinstance(store, FileStorage):
        raise TypeError(f"expected FileStorage, got {type(store).__name__}")
    return store


class MemoryArtifacts:
    """An independent artifact backend with no filesystem delegation."""

    def __init__(self, run_dir):
        self.run_dir = str(run_dir)
        self.data = {}
        self.events = []
        self.manifest = {}
        self.usage = None

    def read_artifact(self, key):
        return deepcopy(self.data.get(key))

    def write_artifact(self, key, value):
        self.data[key] = deepcopy(value)

    def delete_artifact(self, key):
        self.data.pop(key, None)

    def list_artifacts(self, prefix=""):
        return sorted(key for key in self.data if key.startswith(prefix))

    def append_artifact_record(self, key, record):
        self.data.setdefault(key, []).append(deepcopy(record))

    def read_artifact_records(self, key):
        return deepcopy(self.data.get(key, []))

    def state_lock(self):
        return nullcontext()

    def save_manifest(self, data):
        self.manifest = deepcopy(data)

    def save_usage(self, data):
        self.usage = deepcopy(data)

    def load_usage(self):
        return deepcopy(self.usage)

    def record_timing(self, record):
        self.data.setdefault("timing", {})[record["id"]] = record
        return self.data["timing"]

    def log_event(self, event, **data):
        self.events.append({"event": event, **data})


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
            "ingest_config": PreparationService.ingest_config(config),
            "document": document.model_dump(mode="json"),
        },
    )
    changed = config.model_copy(deep=True)
    changed.segment.max_tokens_per_segment += 1
    assert PreparationService.load_parsed_document(store, str(source), changed) is None

    def unexpected_parse(*args, **kwargs):
        raise AssertionError("Preparation re-parsed a matching upload preview")

    monkeypatch.setattr("wenyi_core.pipeline.preparation.load_document", unexpected_parse)
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
