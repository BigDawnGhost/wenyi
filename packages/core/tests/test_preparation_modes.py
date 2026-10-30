"""Shared preparation contracts independent of experimental terminology evidence."""

import json

import pytest
from wenyi_core.agents.analyzer import Analyzer
from wenyi_core.agents.synopsis import Synopsizer
from wenyi_core.config import Config
from wenyi_core.glossary.store import GlossaryTerm
from wenyi_core.pipeline.orchestrator import Orchestrator
from wenyi_core.storage.file import FileStorage
from wenyi_core.storage.sqlite import SqliteStorage

from tests.fake_llm import MeteredFakeClient, routing_handler


@pytest.fixture(params=[FileStorage, SqliteStorage])
def store(request, tmp_path):
    storage = request.param(str(tmp_path / "run"))
    yield storage
    storage.close()


def config(enabled):
    return Config.from_dict(
        {
            "language": {"source": "en", "target": "zh"},
            "llm": {"preset": "fake"},
            "pipeline": {"terminology_context": enabled, "polish": False, "review": False},
        }
    )


def test_preparation_prompts_and_task_identities_are_shared():
    plans = [config(enabled).language_policy("analysis") for enabled in (False, True)]
    for group in ("analyzer", "chapter_digest", "book_synopsis"):
        for role in ("system", "user"):
            task = f"{group}_{role}"
            assert dict(plans[0].templates)[task] == dict(plans[1].templates)[task]
        assert plans[0].task_fingerprint(group) == plans[1].task_fingerprint(group)
    assert not plans[0].enabled("terminology.context")
    assert plans[1].enabled("terminology.context")


@pytest.mark.parametrize("enabled", [False, True])
def test_preparation_agents_cover_source_and_preserve_original_identities(enabled):
    def handler(messages, tier, json_mode):
        if "chapter digest writer" in messages[0]["content"]:
            return "Alice appears. Source-supported digest."
        return routing_handler(messages, tier, json_mode)

    client = MeteredFakeClient(handler=handler)
    selected = config(enabled)
    synopsis = Synopsizer(client, selected)
    source = "Alice waited. " * 1200 + "FINAL_SOURCE_FACT"
    digest = synopsis.digest_chapter(source)
    assert digest
    digest_calls = list(client.calls)
    assert any("FINAL_SOURCE_FACT" in call["messages"][-1]["content"] for call in digest_calls)
    assert len(digest_calls) == 4  # Three source blocks followed by one ordered merge.
    assert all(
        "original source spelling" in call["messages"][0]["content"] for call in digest_calls
    )

    Analyzer(client, selected).analyze("Original prose sample.", chapter_digests=[digest])
    analysis_prompt = client.calls[-1]["messages"][-1]["content"]
    assert digest in analysis_prompt and "Original prose sample." in analysis_prompt
    term = GlossaryTerm("Alice", "艾丽丝", type="person", note="DO_NOT_COPY_FULL_NOTE")
    synopsis.book_synopsis([digest], "Restrained prose.", glossary=[term])
    prompt = client.calls[-1]["messages"]
    assert "original source spellings" in prompt[0]["content"]
    assert '["Alice", "艾丽丝", "person"]' in prompt[-1]["content"]
    assert "DO_NOT_COPY_FULL_NOTE" not in prompt[-1]["content"]
    assert not any(call["operation"].startswith("terminology.") for call in client.calls)


@pytest.mark.parametrize("enabled", [False, True])
def test_initialization_retry_and_mode_switch_reuse_preparation(tmp_path, store, enabled):
    source = tmp_path / "book.txt"
    source.write_text("# Opening\n\nAlice waited.\n\n# Ending\n\nAlice returned.", encoding="utf-8")
    selected = config(enabled)

    def fail_style(messages, tier, json_mode):
        if "pre-translation analyst" in messages[0]["content"]:
            raise RuntimeError("Interrupted style analysis")
        return routing_handler(messages, tier, json_mode)

    first = MeteredFakeClient(handler=fail_style)
    with pytest.raises(RuntimeError, match="Interrupted style"):
        Orchestrator(
            selected, client=first, storage=store, allow_terminology_context=enabled
        ).prepare(str(source))
    assert not store.exists()
    previous = store.load_usage()["totals"]["calls"]
    resumed = MeteredFakeClient(handler=routing_handler)
    Orchestrator(
        selected, client=resumed, storage=store, allow_terminology_context=enabled
    ).prepare_for_translation(str(source))
    assert [call["operation"] for call in resumed.calls] == ["analysis.style", "synopsis.book"]
    assert store.load_usage()["totals"]["calls"] == previous + 2
    assert store.all_terms() == []
    assert store.list_artifacts("terminology/") == []
    chapters = [store.load_chapter(row["index"]) for row in store.load_manifest()["chapters"]]
    assert all(segment.target is None for chapter in chapters for segment in chapter.segments)

    after = store.load_usage()
    switched = MeteredFakeClient(handler=routing_handler)
    Orchestrator(
        config(not enabled),
        client=switched,
        storage=store,
        allow_terminology_context=not enabled,
    ).prepare_for_translation(str(source))
    assert switched.calls == []
    assert store.load_usage() == after
    assert [store.load_chapter(chapter.index) for chapter in chapters] == chapters


@pytest.mark.parametrize("enabled", [False, True])
def test_failed_manifest_commit_reuses_paid_analysis_without_seeding(
    tmp_path, store, monkeypatch, enabled
):
    source = tmp_path / "book.txt"
    source.write_text("Alice waited for the rain.", encoding="utf-8")
    selected = config(enabled)

    def handler(messages, tier, json_mode):
        if "pre-translation analyst" in messages[0]["content"]:
            return json.dumps(
                {
                    "characters": [{"source": "Alice", "target": "Unsolicited translation"}],
                    "terms": [{"source": "rain", "target": "Unsolicited term"}],
                }
            )
        return routing_handler(messages, tier, json_mode)

    def interrupt(manifest):
        raise RuntimeError("Interrupted manifest commit")

    original = store.save_manifest
    monkeypatch.setattr(store, "save_manifest", interrupt)
    first = MeteredFakeClient(handler=handler)
    with pytest.raises(RuntimeError, match="manifest commit"):
        Orchestrator(
            selected, client=first, storage=store, allow_terminology_context=enabled
        ).prepare(str(source))
    assert not store.exists()
    before = store.load_usage()
    monkeypatch.setattr(store, "save_manifest", original)
    resumed = MeteredFakeClient(handler=handler)
    Orchestrator(
        selected, client=resumed, storage=store, allow_terminology_context=enabled
    ).prepare(str(source))
    assert store.exists()
    assert resumed.calls == []
    assert store.all_terms() == []
    assert "terms" not in store.load_analysis()
    assert store.load_usage() == before
