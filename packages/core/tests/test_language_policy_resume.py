"""Policy revisions preserve formal targets and invalidate only affected paid work."""

import pytest
from wenyi_core.config import Config
from wenyi_core.glossary.store import GlossaryTerm
from wenyi_core.i18n.resources import read_json, read_text
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.pipeline.orchestrator import Orchestrator
from wenyi_core.pipeline.run.language_policies import CHECKPOINT
from wenyi_core.storage.file import FileStorage

from tests.fake_llm import routing_handler


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


@pytest.mark.parametrize("cached", [False, True])
def test_missing_identity_rebuilds_analysis_and_failed_rebuild_is_resumable(
    tmp_path, monkeypatch, cached
):
    source, config, store = book(tmp_path)
    Orchestrator(config, client=FakeClient(handler=routing_handler), storage=store).run(str(source))
    saved = [store.load_chapter(index).model_dump() for index in (0, 1)]
    store.write_artifact(CHECKPOINT, {})
    analysis = store.load_analysis()
    assert analysis is not None
    analysis.pop("style_policy")
    analysis.pop("language_policy")
    store.save_analysis(analysis)
    if not cached:
        for key in store.list_artifacts("preparation/analysis/"):
            store.delete_artifact(key)
    client = FakeClient(handler=routing_handler)
    resumed = Orchestrator(config, client=client, storage=store)
    monkeypatch.setattr(
        resumed._runtime.analyzer,
        "analyze",
        lambda sample: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    if cached:
        resumed.run(str(source))
        assert client.calls == []
        assert store.read_artifact(CHECKPOINT) != {}
        assert [store.load_chapter(index).model_dump() for index in (0, 1)] == saved
        return
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


def test_target_language_revision_preserves_saved_translations_and_glossary(tmp_path, monkeypatch):
    source, config, store = book(tmp_path)
    Orchestrator(config, client=FakeClient(handler=routing_handler), storage=store).run(
        str(source), only_chapter=0
    )
    store.upsert_term(GlossaryTerm(source="灯台", target="灯塔", note="User-approved wording."))
    saved_chapter = store.load_chapter(0).model_dump()
    saved_analysis = store.load_analysis()
    saved_terms = store.all_terms()
    monkeypatch.setattr(
        "wenyi_core.i18n.policy.resolver.read_text",
        lambda path: (
            read_text(path)
            + (
                "\nUse the revised target-language requirements."
                if path == "shared/target_language.txt"
                else ""
            )
        ),
    )
    client = FakeClient(handler=routing_handler)
    Orchestrator(config, client=client, storage=store).run(str(source))
    assert store.load_chapter(0).model_dump() == saved_chapter
    assert store.load_analysis() == saved_analysis
    assert store.all_terms() == saved_terms
    assert all(segment.target is not None for segment in store.load_chapter(1).text_segments)
    assert "analysis.style" not in [call["operation"] for call in client.calls]
    call_count = len(client.calls)
    Orchestrator(config, client=client, storage=store).run(str(source))
    assert len(client.calls) == call_count


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
