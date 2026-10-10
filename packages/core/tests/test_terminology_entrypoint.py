"""Generation permission is explicit; existing-note consumption remains independent."""

from unittest.mock import Mock, patch

import pytest
from typer.testing import CliRunner
from wenyi_cli.cli import app
from wenyi_core.agents.analyzer import Analyzer
from wenyi_core.config import Config
from wenyi_core.glossary.store import GlossaryTerm
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.pipeline.autofix_verification import AutofixVerification
from wenyi_core.pipeline.orchestrator import Orchestrator
from wenyi_core.pipeline.review_rounds import ReviewRoundService
from wenyi_core.review.evidence import BookEvidenceIndex
from wenyi_core.review.models import ReviewOutcome
from wenyi_core.review.run_store import ReviewRunStore
from wenyi_core.review.session import ReviewRoundResult
from wenyi_core.storage.file import FileStorage

from tests.fake_llm import routing_handler
from tests.test_storage_injection import MemoryArtifacts
from tests.test_terminology_pipeline import FamilyHandler


def config(tmp_path, *, enabled=True):
    return Config.from_dict(
        {
            "language": {"source": "en", "target": "zh"},
            "llm": {"preset": "fake"},
            "pipeline": {
                "terminology_context": enabled,
                "review": False,
                "polish": False,
                "book_understanding": False,
                "review_agent_loop": False,
                "review_fix_loop": False,
            },
            "paths": {
                "state_dir": str(tmp_path / "state"),
                "output_dir": str(tmp_path / "output"),
            },
        }
    )


@pytest.mark.parametrize("entry", ["prepare", "prepare_for_translation", "run", "run_all"])
@pytest.mark.parametrize("injected", [False, True])
def test_non_cli_generation_fails_before_parsing_calls_or_artifacts(tmp_path, entry, injected):
    client = FakeClient(handler=routing_handler)
    storage = MemoryArtifacts(tmp_path / "resources") if injected else None
    orch = Orchestrator(config(tmp_path), client=client, storage=storage)
    with pytest.raises(ValueError, match="supported only by CLI prepare/translate"):
        getattr(orch, entry)(str(tmp_path / "missing.txt"))
    assert client.calls == []
    assert not (tmp_path / "state").exists()
    if storage is not None:
        assert storage.data == {}
        assert storage.events == []
        assert storage.manifest == {}


def test_permission_does_not_enable_the_feature_when_config_is_disabled(tmp_path):
    source = tmp_path / "book.txt"
    source.write_text("# Chapter 1\n\nAlice enters.\n\nAlice waits.", encoding="utf-8")
    client = FakeClient(handler=routing_handler)
    store = Orchestrator(
        config(tmp_path, enabled=False), client=client, allow_terminology_context=True
    ).run(str(source))
    assert all(segment.target is not None for segment in store.load_chapter(0).text_segments)
    assert not any(call["operation"].startswith("terminology.") for call in client.calls)
    assert store.list_artifacts("terminology/") == []


@pytest.mark.parametrize("entry", ["prepare", "translate"])
def test_real_cli_entry_produces_terminology_evidence(tmp_path, entry):
    source = tmp_path / "book.txt"
    source.write_text("# Chapter 1\n\nAlice enters.\n\nAlice waits.", encoding="utf-8")
    selected = config(tmp_path)
    client = FakeClient(handler=FamilyHandler())
    args = [entry, str(source)]
    if entry == "translate":
        args += ["--format", "txt", "--out", str(tmp_path / "translation.txt")]
    with (
        patch("wenyi_cli.commands.context.CommandContext.load_config", return_value=selected),
        patch("wenyi_core.pipeline.runtime.build_client", return_value=client),
    ):
        result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert any(call["operation"] == "terminology.evidence" for call in client.calls)
    manifests = list((tmp_path / "state").glob("*/targets/zh/manifest.json"))
    assert len(manifests) == 1
    store = FileStorage(str(manifests[0].parent))
    with store.lock():
        assert store.get_term("Alice").note.endswith("全书人物说明。")
        assert store.list_artifacts("terminology/requests/evidence/")
        completed = all(
            segment.target is not None for segment in store.load_chapter(0).text_segments
        )
        assert completed == (entry == "translate")


@pytest.mark.parametrize("entry", ["run_review", "run_report", "run_assemble"])
def test_existing_state_operations_consume_notes_without_generating_evidence(tmp_path, entry):
    source = tmp_path / "book.txt"
    source.write_text("# Chapter 1\n\nAlice enters.\n\nAlice waits.", encoding="utf-8")
    store = FileStorage(str(tmp_path / "run"))
    Orchestrator(
        config(tmp_path, enabled=False), client=FakeClient(handler=routing_handler), storage=store
    ).run(str(source))
    note = "An operator-approved local note."
    with store.lock():
        store.upsert_term(GlossaryTerm("Alice", "爱丽丝", note=note))
        before = store.load_chapter(0)
        terms = store.all_terms()
    client = FakeClient(handler=routing_handler)
    orch = Orchestrator(config(tmp_path), client=client, storage=store)
    kwargs = (
        {"out_format": "txt", "out_path": str(tmp_path / "translation.txt")}
        if entry == "run_assemble"
        else {}
    )
    getattr(orch, entry)(str(source), **kwargs)
    assert not any(call["operation"].startswith("terminology.") for call in client.calls)
    assert store.list_artifacts("terminology/") == []
    if entry == "run_review":
        assert any(
            note in message["content"] for call in client.calls for message in call["messages"]
        )
    else:
        assert client.calls == []
    with store.lock():
        assert store.load_chapter(0) == before
        assert store.all_terms() == terms


@pytest.mark.parametrize("enabled", [False, True])
def test_translation_retains_legacy_character_notes_when_feature_is_disabled(tmp_path, enabled):
    source = tmp_path / "book.txt"
    source.write_text("# Chapter 1\n\nAlice enters.\n\nAlice waits.", encoding="utf-8")
    store = FileStorage(str(tmp_path / "run"))
    Orchestrator(
        config(tmp_path, enabled=False), client=FakeClient(handler=FamilyHandler()), storage=store
    ).prepare(str(source))
    with store.lock():
        analysis = store.load_analysis()
        analysis["characters"][0]["note"] = "LEGACY_CHARACTER_NOTE"
        store.save_analysis(analysis)
        store.upsert_term(
            GlossaryTerm("Alice", "艾丽丝", type="person", note="LOCAL_GLOSSARY_NOTE")
        )
    client = FakeClient(handler=FamilyHandler())
    Orchestrator(
        config(tmp_path, enabled=enabled),
        client=client,
        storage=store,
        allow_terminology_context=True,
    ).run(str(source))
    prompts = [
        call["messages"][1]["content"]
        for call in client.calls
        if call["operation"] == "translation.body"
    ]
    assert prompts
    assert all(("LEGACY_CHARACTER_NOTE" in text) == (not enabled) for text in prompts)
    assert all(("LOCAL_GLOSSARY_NOTE" in text) == enabled for text in prompts)


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("stage", ["review", "autofix"])
def test_revision_style_keeps_legacy_characters_without_the_opt_in_note_channel(
    tmp_path, enabled, stage
):
    selected = config(tmp_path, enabled=enabled)
    client = FakeClient()
    analysis = {
        "style_guide": "Restrained.",
        "characters": [{"source": "Alice", "target": "艾丽丝", "note": "LEGACY_CHARACTER_NOTE"}],
    }
    style_brief = Mock(wraps=Analyzer(client, selected).style_brief)
    evidence = BookEvidenceIndex([], [], analysis)
    debug = ReviewRunStore(str(tmp_path / "review"))
    if stage == "review":
        result = ReviewRoundResult(
            issues=[{"chapter": 0, "index": 0, "issue_id": "issue"}],
            pre_arbitration_issues=[],
            arbitration_superseded=[],
            conflict_groups=[],
            residual_conflicts=[],
            fallback_agent_count=0,
        )
        ReviewRoundService(selected, client, style_brief, Mock()).propose_review_patches(
            result, evidence, [], analysis, debug, review_round=1, fix_round=1
        )
    else:
        verification = AutofixVerification(
            selected,
            client,
            evidence,
            debug,
            [],
            analysis,
            ReviewOutcome(debug.run_dir, {}, {}),
            style_brief,
        )
        assert ("LEGACY_CHARACTER_NOTE" in verification.style) == (not enabled)
    style_brief.assert_called_once_with({**analysis, "characters": []} if enabled else analysis)
    assert analysis["characters"][0]["note"] == "LEGACY_CHARACTER_NOTE"
    assert client.calls == []
