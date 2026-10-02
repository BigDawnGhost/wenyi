"""Policy revisions preserve formal targets and invalidate only affected paid work."""

import pytest
from wenyi_core.config import Config
from wenyi_core.i18n.resources import read_json, read_text
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.pipeline.language_policies import CHECKPOINT
from wenyi_core.pipeline.orchestrator import Orchestrator
from wenyi_core.storage.file import FileStorage

from tests.fake_llm import routing_handler


def test_subtitle_revision_keeps_completed_cues_and_rejects_old_window_cache(tmp_path, monkeypatch):
    import json

    from wenyi_core.srt.store import SrtRunStore
    from wenyi_core.srt.translate import translate_srt

    source = tmp_path / "sample.srt"
    source.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nOne.\n\n"
        "2\n00:00:02,000 --> 00:00:03,000\nTwo.\n\n"
        "3\n00:00:03,000 --> 00:00:04,000\nThree.\n\n"
    )
    config = Config.from_dict(
        {
            "language": {"source": "en", "target": "zh"},
            "llm": {"preset": "fake"},
            "paths": {"state_dir": str(tmp_path / "state")},
        }
    )
    store = SrtRunStore.for_source(config.state_dir, str(source), "zh")
    plan = config.language_policy("translation", path="srt")
    store.ensure_manifest(str(source), cue_count=3, source_lang="en", plan=plan)
    cues = store.ensure_cues(
        [
            ("1", "00:00:01,000 --> 00:00:02,000", "One."),
            ("2", "00:00:02,000 --> 00:00:03,000", "Two."),
            ("3", "00:00:03,000 --> 00:00:04,000", "Three."),
        ]
    )
    store.save_cues(store.apply_translations(cues, {"1": "保留人工译文。"}))
    store.save_batch(0, {"1": "旧缓存一", "2": "旧缓存二", "3": "旧缓存三"})
    monkeypatch.setattr(
        "wenyi_core.i18n.policy.resolver.read_text",
        lambda path: (
            read_text(path)
            + ("\nKeep source evidence." if path == "tasks/srt_batch_system.txt" else "")
        ),
    )
    client = FakeClient(
        handler=lambda messages, tier, json_mode: json.dumps(
            {"1": "新译文一", "2": "新译文二", "3": "新译文三"}, ensure_ascii=False
        )
    )
    result = translate_srt(str(source), config, client=client)
    assert result["translated"] == 3
    assert [call["operation"] for call in client.calls] == ["srt.translate"]
    assert {index: row["target"] for index, row in store.load_cues().items()} == {
        "1": "保留人工译文。",
        "2": "新译文二",
        "3": "新译文三",
    }
    export_events = [
        row
        for row in store._artifacts.read_artifact_records("events.jsonl")
        if row.get("event") == "language_policy_resolved" and row.get("phase") == "export"
    ]
    assert len(export_events) == 1
    export = store.read_artifact(export_events[0]["artifact"])
    assert export is not None
    assert export["context"]["path"] == "srt"
    assert export["context"]["format"] == "srt"
    assert export["context"]["source_identity"]
    assert all(not operation["enabled"] for operation in export["selections"])


def book(tmp_path):
    source = tmp_path / "book.txt"
    source.write_text("# Opening\n\n扉が開いた。\n\n# Ending\n\n彼は戻った。\n", encoding="utf-8")
    config = Config.from_dict(
        {
            "language": {"source": "ja", "target": "zh"},
            "llm": {"preset": "fake"},
            "pipeline": {
                "book_understanding": False,
                "polish": False,
                "review": False,
                "review_agent_loop": False,
                "review_fix_loop": False,
            },
            "paths": {"state_dir": str(tmp_path / "state")},
        }
    )
    return source, config, FileStorage(str(tmp_path / "run"))


@pytest.mark.parametrize("fresh_process", [False, True])
def test_changed_semantic_revision_refreshes_pending_work_and_keeps_completed_targets(
    tmp_path, fresh_process
):
    source, config, store = book(tmp_path)
    initial_client = FakeClient(handler=routing_handler)
    initial = Orchestrator(config, client=initial_client, storage=store)
    initial.run(str(source), only_chapter=0)
    saved = store.load_chapter(0).model_dump()
    initial_manifest = store.load_manifest()
    config.honorific_strategy = "normalize"
    client = FakeClient(handler=routing_handler) if fresh_process else initial_client
    previous_calls = len(client.calls)
    resumed = Orchestrator(config, client=client, storage=store) if fresh_process else initial
    resumed.run(str(source))
    assert store.load_chapter(0).model_dump() == saved
    assert all(segment.target is not None for segment in store.load_chapter(1).text_segments)
    assert "analysis.style" not in [call["operation"] for call in client.calls[previous_calls:]]
    assert store.load_manifest()["language_policies"] == initial_manifest["language_policies"]


def test_missing_identity_rebuilds_analysis_and_failed_rebuild_is_resumable(tmp_path, monkeypatch):
    source, config, store = book(tmp_path)
    Orchestrator(config, client=FakeClient(handler=routing_handler), storage=store).run(str(source))
    saved = [store.load_chapter(index).model_dump() for index in (0, 1)]
    store.write_artifact(CHECKPOINT, {})
    analysis = store.load_analysis()
    assert analysis is not None
    analysis.pop("style_policy")
    analysis.pop("language_policy")
    store.save_analysis(analysis)
    client = FakeClient(handler=routing_handler)
    resumed = Orchestrator(config, client=client, storage=store)
    monkeypatch.setattr(
        resumed._runtime.analyzer,
        "analyze",
        lambda sample: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    with pytest.raises(KeyboardInterrupt):
        resumed.run(str(source))
    assert store.read_artifact(CHECKPOINT) == {}
    assert [store.load_chapter(index).model_dump() for index in (0, 1)] == saved
    client = FakeClient(handler=routing_handler)
    Orchestrator(config, client=client, storage=store).run(str(source))
    assert [call["operation"] for call in client.calls] == ["analysis.style"]
    assert [store.load_chapter(index).model_dump() for index in (0, 1)] == saved


def test_builtin_analysis_change_rebuilds_analysis_without_retranslating(tmp_path, monkeypatch):
    source, config, store = book(tmp_path)
    Orchestrator(config, client=FakeClient(handler=routing_handler), storage=store).run(str(source))
    saved = [store.load_chapter(index).model_dump() for index in (0, 1)]
    previous = store.read_artifact(CHECKPOINT)
    assert previous is not None
    old_translation = config.language_policy().fingerprint
    monkeypatch.setattr(
        "wenyi_core.i18n.policy.resolver.read_text",
        lambda path: (
            read_text(path)
            + ("\nFollow source evidence." if path == "tasks/analyzer_system.txt" else "")
        ),
    )
    client = FakeClient(handler=routing_handler)
    Orchestrator(config, client=client, storage=store).run(str(source))
    assert config.language_policy().fingerprint == old_translation
    assert [call["operation"] for call in client.calls] == ["analysis.style"]
    assert [store.load_chapter(index).model_dump() for index in (0, 1)] == saved
    current = store.read_artifact(CHECKPOINT)
    assert current is not None
    assert current["analysis"] != previous["analysis"]
    assert current["translation"] == previous["translation"]


def test_builtin_font_and_unused_languages_do_not_invalidate_paid_checkpoints(
    tmp_path, monkeypatch
):
    source, config, store = book(tmp_path)
    Orchestrator(config, client=FakeClient(handler=routing_handler), storage=store).run(str(source))
    reference = store.read_artifact(CHECKPOINT)
    queried = []

    def revised_json(path):
        queried.append(path)
        data = read_json(path)
        if path == "languages/zh.json":
            data["policy"]["target"]["docx.chinese_font"]["options"] = {"font": "Arial"}
        return (
            {**data, "target_guidance": "unused revision"} if path == "languages/vi.json" else data
        )

    monkeypatch.setattr("wenyi_core.i18n.languages.read_json", revised_json)
    monkeypatch.setattr("wenyi_core.i18n.policy.resolver.read_json", revised_json)
    client = FakeClient(handler=routing_handler)
    Orchestrator(config, client=client, storage=store).run(str(source))
    assert client.calls == []
    assert store.read_artifact(CHECKPOINT) == reference
    assert "languages/vi.json" not in queried
    export = Orchestrator(config, client=client, storage=store).run_assemble(
        str(source), out_format="docx"
    )
    assert export["outputs"]
    assert client.calls == []


def test_unavailable_pinned_handler_is_rejected_before_calls(tmp_path):
    source, config, store = book(tmp_path)
    Orchestrator(config, client=FakeClient(handler=routing_handler), storage=store).prepare(
        str(source)
    )
    checkpoint = store.read_artifact(CHECKPOINT)
    assert checkpoint is not None
    reference = checkpoint["translation"]
    artifact = store.read_artifact(reference)
    assert artifact is not None
    artifact["selections"][0]["implementation_version"] = "unavailable"
    store.write_artifact(reference, artifact)
    client = FakeClient(handler=routing_handler)
    with pytest.raises(ValueError, match="Pinned language operation is unavailable"):
        Orchestrator(config, client=client, storage=store).run(str(source))
    assert client.calls == []


def test_invalid_chapter_is_rejected_before_changed_policy_rebuild(tmp_path, monkeypatch):
    source, config, store = book(tmp_path)
    Orchestrator(config, client=FakeClient(handler=routing_handler), storage=store).prepare(
        str(source)
    )
    checkpoint = store.read_artifact(CHECKPOINT)
    monkeypatch.setattr(
        "wenyi_core.i18n.policy.resolver.read_text",
        lambda path: (
            read_text(path)
            + ("\nFollow source evidence." if path == "tasks/analyzer_system.txt" else "")
        ),
    )
    client = FakeClient(handler=routing_handler)
    with pytest.raises(ValueError, match="Chapter index 99 does not exist"):
        Orchestrator(config, client=client, storage=store).run(str(source), only_chapter=99)
    assert client.calls == []
    assert store.read_artifact(CHECKPOINT) == checkpoint
