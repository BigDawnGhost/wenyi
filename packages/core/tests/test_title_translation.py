"""Title reuse, bounded requests and incremental manifest commits."""

import json
import re
from contextlib import closing

import pytest
from wenyi_core.agents.prompts import numbered
from wenyi_core.config import Config
from wenyi_core.glossary.store import GlossaryStore
from wenyi_core.i18n.prompts import render
from wenyi_core.ingest.models import Chapter, Document, Segment
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.pipeline.orchestrator import Orchestrator
from wenyi_core.pipeline.title_translation import plan_titles
from wenyi_core.storage.file import FileStorage
from wenyi_core.storage.protocol import Storage


def require_file_storage(store: Storage) -> FileStorage:
    """CLI/offline tests use the file backend; narrow Storage to FileStorage for path asserts."""
    if not isinstance(store, FileStorage):
        raise TypeError(f"expected FileStorage, got {type(store).__name__}")
    return store


def _titles(messages):
    return re.findall(r"^\[\d+\] (.*)$", messages[-1]["content"], re.MULTILINE)


@pytest.fixture
def title_project(tmp_path):
    config = Config.from_dict(
        {"llm": {"preset": "fake"}, "language": {"source": "en", "target": "zh"}}
    )
    store = FileStorage(str(tmp_path / "state"))
    with closing(GlossaryStore(store.glossary_path)) as glossary:

        def initialize(chapters, meta=None):
            manifest = store.stage_document(
                Document(
                    title="Original book title",
                    source_lang="en",
                    target_lang="zh",
                    fmt="txt",
                    chapters=chapters,
                    meta=meta or {},
                ),
                source_hash="a" * 64,
            )
            store.save_manifest(manifest)
            return manifest

        yield config, store, glossary, initialize


def _run_titles(config, store, glossary, client):
    Orchestrator(config, client)._translation._titles.run(store, glossary)


def test_title_plan_keeps_uncommitted_changes_out_of_its_input_manifest():
    manifest = {"title": "Original", "chapters": [{"index": 0, "title": "Pending"}]}
    plan = plan_titles(manifest, {0: Chapter(index=0, title="Pending")})
    plan.apply(plan.pending, ["已翻译"])
    assert plan.manifest["chapters"][0]["title_translated"] == "已翻译"
    assert manifest == {"title": "Original", "chapters": [{"index": 0, "title": "Pending"}]}


def test_title_agent_uses_restored_languages_and_the_existing_request_protocol():
    config = Config.from_dict(
        {"llm": {"preset": "fake"}, "language": {"source": "auto", "target": "zh"}}
    )
    client = FakeClient(handler=lambda *args: '{"titles": [" 开篇 "]}')
    runtime = Orchestrator(config, client)._runtime
    runtime.apply_manifest_languages({"source_lang": "fr", "target_lang": "zh"})
    assert runtime.title_translator.translate(["Le début"], "A term") == ["开篇"]
    assert client.calls[0]["operation"] == "translation.title"
    assert client.calls[0]["messages"] == [
        {
            "role": "system",
            "content": render("title_translator_system", src="fr", tgt="zh", n=1),
        },
        {
            "role": "user",
            "content": render(
                "title_translator_user",
                src="fr",
                tgt="zh",
                n=1,
                glossary="A term",
                numbered_titles=numbered(["Le début"]),
            ),
        },
    ]


def test_resume_only_reissues_uncommitted_title_batch(title_project):
    config, store, glossary, initialize = title_project
    initialize([Chapter(index=i, title=f"Title {i}") for i in range(43)])
    batches = []

    def handler(messages, tier, json_mode):
        titles = _titles(messages)
        batches.append(titles)
        if len(batches) == 2:
            raise RuntimeError("interrupted")
        return json.dumps({"titles": ["Translated " + title for title in titles]})

    client = FakeClient(handler=handler)
    with pytest.raises(RuntimeError, match="interrupted"):
        _run_titles(config, store, glossary, client)
    saved = store.load_manifest()
    assert [len(batch) for batch in batches] == [40, 3]
    assert all(row.get("title_translated") for row in saved["chapters"][:40])
    assert all("title_translated" not in row for row in saved["chapters"][40:])

    resumed = FakeClient(handler=handler)
    _run_titles(config, store, glossary, resumed)
    assert len(resumed.calls) == 1
    assert _titles(resumed.calls[0]["messages"]) == [f"Title {i}" for i in range(40, 43)]
    assert resumed.calls[0]["operation"] == "translation.title"
    assert store.load_manifest()["title"] == "Original book title"
    before = store.load_manifest()
    _run_titles(config, store, glossary, FakeClient())
    assert store.load_manifest() == before


def test_title_requests_preserve_character_budget_and_location_duplicates(title_project):
    config, store, glossary, initialize = title_project
    titles = ["a" * 2000, "b" * 2000, "Repeated", "Repeated", "c" * 4001, "Last\n title"]
    initialize([Chapter(index=i, title=title) for i, title in enumerate(titles)])
    client = FakeClient(handler=lambda messages, *args: json.dumps({"titles": _titles(messages)}))
    _run_titles(config, store, glossary, client)
    assert [_titles(call["messages"]) for call in client.calls] == [
        titles[:2],
        titles[2:4],
        [titles[4]],
        ["Last title"],
    ]


def test_reuse_complete_heading_continuations_without_changing_book_identity(title_project):
    config, store, glossary, initialize = title_project
    chapter = Chapter(
        index=0,
        title="The opening",
        segments=[
            Segment(index=0, source="The ", target="开", kind="heading", anchor="tn0_0"),
            Segment(index=1, source="opening", target="篇", kind="heading", cont=True),
        ],
    )
    manifest = initialize(
        [chapter],
        {
            "toc_entries": [
                {"entry_id": "nav:0", "title": "The opening", "segment_anchor": "tn0_0"},
                {"entry_id": "nav:1", "title": "Another title", "segment_anchor": "tn0_0"},
            ]
        },
    )
    manifest["chapters"][0]["toc_entry_id"] = "nav:0"
    store.save_manifest(manifest)
    client = FakeClient(handler=lambda *args: '{"titles": ["另一个标题"]}')
    _run_titles(config, store, glossary, client)
    saved = store.load_manifest()
    assert _titles(client.calls[0]["messages"]) == ["Another title"]
    assert saved["chapters"][0]["title_translated"] == "开篇"
    assert saved["meta"]["toc_entries"][0]["title_translated"] == "开篇"
    assert saved["title"] == "Original book title"
    assert store.load_chapter(0) == chapter


@pytest.mark.parametrize("response", [{"titles": []}, {"wrong": ["unused"]}, "not a list"])
def test_rejected_title_response_does_not_commit_batch(title_project, response):
    config, store, glossary, initialize = title_project
    before = initialize([Chapter(index=0, title="A title")])
    with pytest.raises(RuntimeError, match="invalid number of items"):
        _run_titles(config, store, glossary, FakeClient(handler=lambda *args: json.dumps(response)))
    assert store.load_manifest() == before


def test_title_string_conversion_and_empty_fallback_remain_unchanged(title_project):
    config, store, glossary, initialize = title_project
    initialize([Chapter(index=i, title=f"Title {i}") for i in range(3)])
    _run_titles(
        config, store, glossary, FakeClient(handler=lambda *args: '{"titles": ["  ", 3, " T "]}')
    )
    assert [row["title_translated"] for row in store.load_manifest()["chapters"]] == [
        "Title 0",
        "3",
        "T",
    ]
