"""Title reuse, bounded requests and incremental manifest commits."""

import json
import re
from contextlib import closing
from copy import deepcopy
from unittest.mock import Mock

import pytest
from wenyi_core.agents.prompts import numbered
from wenyi_core.config import Config
from wenyi_core.glossary.store import GlossaryStore
from wenyi_core.i18n.prompts import render
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.models.document import Chapter, Document, Segment
from wenyi_core.pipeline.orchestrator import Orchestrator
from wenyi_core.pipeline.translate.title_translation import plan_titles
from wenyi_core.storage.file import FileStorage
from wenyi_core.storage.protocol import Storage


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


@pytest.mark.parametrize("pending_metadata", [False, True], ids=["complete", "manifest-only"])
def test_titles_without_heading_candidates_do_not_load_chapters(tmp_path, pending_metadata):
    manifest = {
        "title": "Chapter",
        "fmt": "epub",
        "chapters": [
            {"index": 0, "title": "Chapter", "title_translated": "章节", "toc_entry_id": "nav:0"}
        ],
        "meta": {
            "opf_path": "EPUB/package.opf",
            "book_title_translated": "章节",
            "toc_entries": [
                {
                    "entry_id": "nav:0",
                    "title": "Chapter",
                    "segment_anchor": "tn0_0",
                    "title_translated": "章节",
                }
            ],
            "epub_subjects": ["Fiction", "Drama"],
            "epub_subjects_translated": ["小说", "剧情"],
        },
    }
    if pending_metadata:
        del manifest["chapters"][0]["title_translated"]
        manifest["chapters"].append({"index": 1, "title": " "})
        del manifest["meta"]["book_title_translated"]
        manifest["meta"]["toc_entries"].append({"entry_id": "nav:1", "title": "Appendix"})
        manifest["meta"]["epub_subjects_translated"][1] = ""

    saved = []
    store = Mock(spec=Storage)
    store.load_manifest.return_value = manifest
    store.load_chapter.side_effect = lambda index: Chapter(index=index)
    store.save_manifest.side_effect = lambda value: saved.append(deepcopy(value))
    config = Config.from_dict(
        {"llm": {"preset": "fake"}, "language": {"source": "en", "target": "zh"}}
    )
    client = FakeClient(handler=lambda *args: '{"titles": ["附录", "剧情"]}')
    with closing(GlossaryStore(str(tmp_path / "glossary.db"))) as glossary:
        _run_titles(config, store, glossary, client)
        terms = [(term.source, term.target) for term in glossary.all_terms()]

    store.load_chapter.assert_not_called()
    if pending_metadata:
        assert [_titles(call["messages"]) for call in client.calls] == [["Appendix", "Drama"]]
        reused, completed = saved
        assert reused["chapters"][0]["title_translated"] == "章节"
        assert reused["meta"]["book_title_translated"] == "章节"
        assert reused["meta"]["epub_subjects_translated"] == ["小说", ""]
        assert completed["meta"]["epub_subjects_translated"] == ["小说", "剧情"]
        assert terms == [("Chapter", "章节"), ("Appendix", "附录")]
    else:
        assert client.calls == []
        assert saved == []
        assert terms == [("Chapter", "章节")]
        store.log_event.assert_called_once_with(
            "titles_skipped", reason="already_translated_or_reused"
        )


def test_title_completion_callback_type_error_propagates_without_retry(title_project):
    config, store, glossary, initialize = title_project
    initialize([Chapter(index=0, title="A title")])
    client = FakeClient(handler=lambda *args: '{"titles": ["标题"]}')
    service = Orchestrator(config, client)._translation._titles
    error = TypeError("raised inside the callback")
    completion_payloads = []

    def progress(done: int, total: int, label: str, payload: dict | None = None) -> None:
        if done:
            assert store.load_manifest()["chapters"][0]["title_translated"] == "标题"
            completion_payloads.append(payload)
            raise error

    with pytest.raises(TypeError) as raised:
        service.run(store, glossary, progress=progress)

    assert raised.value is error
    assert completion_payloads == [{"batch": 0, "batch_size": 1}]


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


@pytest.mark.parametrize("chapter_title", [None, "手定标题"])
def test_reuse_complete_heading_continuations_without_changing_book_identity(
    title_project, chapter_title
):
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
    if chapter_title is None:
        manifest["chapters"][0]["toc_entry_id"] = "nav:0"
    else:
        manifest["chapters"][0]["title_translated"] = chapter_title
    store.save_manifest(manifest)
    client = FakeClient(handler=lambda *args: '{"titles": ["另一个标题"]}')
    _run_titles(config, store, glossary, client)
    saved = store.load_manifest()
    assert _titles(client.calls[0]["messages"]) == ["Another title"]
    assert saved["chapters"][0]["title_translated"] == (chapter_title or "开篇")
    assert saved["meta"]["toc_entries"][0]["title_translated"] == "开篇"
    assert saved["title"] == "Original book title"
    assert store.load_chapter(0) == chapter


def test_reuse_first_heading_when_linked_toc_has_no_anchor():
    manifest = {
        "chapters": [{"index": 0, "title": "Opening", "toc_entry_id": "nav:0"}],
        "meta": {"toc_entries": [{"entry_id": "nav:0", "title": "Part One"}]},
    }
    load_chapters = Mock(
        return_value={
            0: Chapter(
                index=0,
                segments=[
                    Segment(index=0, source="Opening", target="开篇", kind="heading", anchor="h0")
                ],
            )
        }
    )

    plan = plan_titles(manifest, load_chapters)

    load_chapters.assert_called_once_with()
    assert plan.manifest["chapters"][0]["title_translated"] == "开篇"
    assert [item.source for item in plan.pending] == ["Part One"]


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


def test_title_translator_resamples_structurally_repaired_reply():
    from wenyi_core.agents.title_translator import TitleTranslator
    from wenyi_core.llm.json_parser import UnsafeJsonRepairError

    leaked = '{"titles":["Xishan Baishi</titles>}Wait, JSON only.{'
    replies = [leaked, '{"titles":["Xishan Baishi"]}']
    client = FakeClient(handler=lambda messages, tier, json_mode: replies.pop(0))
    config = Config.from_dict(
        {"language": {"source": "zh", "target": "en"}, "llm": {"preset": "fake"}}
    )

    assert TitleTranslator(client, config).translate(["夕山白石"], "") == ["Xishan Baishi"]

    client = FakeClient(handler=lambda messages, tier, json_mode: leaked)
    with pytest.raises(UnsafeJsonRepairError):
        TitleTranslator(client, config).translate(["夕山白石"], "")
    assert len(client.calls) == 2


def test_plan_titles_queues_epub_creators_and_nav_titles():
    manifest = {
        "title": "特拉福买家俱乐部",
        "fmt": "epub",
        "chapters": [],
        "meta": {
            "opf_path": "OEBPS/content.opf",
            "book_title_translated": "The Trafford Buyers Club",
            "epub_creators": ["夕山白石"],
            "epub_nav_titles": ["目录"],
        },
    }
    plan = plan_titles(manifest, {})
    assert [(item.field, item.source, item.list_index) for item in plan.pending] == [
        ("epub_creators_translated", "夕山白石", 0),
        ("epub_nav_titles_translated", "目录", 0),
    ]
    plan.apply(plan.pending, ["Xishan Baishi", "Contents"])
    assert plan.manifest["meta"]["epub_creators_translated"] == ["Xishan Baishi"]
    assert plan.manifest["meta"]["epub_nav_titles_translated"] == ["Contents"]
    assert plan_titles(plan.manifest, {}).pending == []


def test_plan_titles_scopes_opf_metadata_and_handles_subject_resume():
    manifest = {
        "title": "Shadow Empire",
        "fmt": "epub",
        "chapters": [{"index": 0, "title": "Chapter 1", "title_translated": "第一章"}],
        "meta": {
            "opf_path": "OEBPS/content.opf",
            "epub_description": "A story.",
            "epub_subjects": ["Fiction", "Drama"],
            "epub_subjects_translated": ["小说", ""],
        },
    }
    plan = plan_titles(manifest, {})
    assert [(item.field, item.source, item.list_index) for item in plan.pending] == [
        ("book_title_translated", "Shadow Empire", None),
        ("epub_subjects_translated", "Drama", 1),
    ]
    plan.apply(plan.pending, ["阴影帝国", "剧情"])
    assert plan.manifest["title"] == "Shadow Empire"
    assert plan.manifest["meta"]["book_title_translated"] == "阴影帝国"
    assert "epub_description_translated" not in plan.manifest["meta"]
    assert plan.manifest["meta"]["epub_subjects_translated"] == ["小说", "剧情"]

    # Truncated subject list must re-plan all entries rather than wiping skipped prefix slots.
    truncated = {
        "title": "Shadow Empire",
        "fmt": "epub",
        "chapters": [],
        "meta": {
            "opf_path": "OEBPS/content.opf",
            "book_title_translated": "阴影帝国",
            "epub_subjects": ["Fiction", "Drama"],
            "epub_subjects_translated": ["小说"],
        },
    }
    truncated_plan = plan_titles(truncated, {})
    assert [(item.source, item.list_index) for item in truncated_plan.pending] == [
        ("Fiction", 0),
        ("Drama", 1),
    ]
    truncated_plan.apply(truncated_plan.pending, ["小说", "剧情"])
    assert truncated_plan.manifest["meta"]["epub_subjects_translated"] == ["小说", "剧情"]

    # MinerU PDFs (fmt='pdf' with opf_path) and EPUB manifests without opf_path skip OPF metadata.
    for non_opf in (
        {
            "title": "paper-stem",
            "fmt": "pdf",
            "chapters": [{"index": 0, "title": "Intro", "title_translated": "引言"}],
            "meta": {"opf_path": "EPUB/package.opf", "mineru_epub_sha256": "a" * 64},
        },
        {
            "title": "Synthetic",
            "fmt": "epub",
            "chapters": [{"index": 0, "title": "Intro", "title_translated": "引言"}],
            "meta": {"toc_entries": []},
        },
    ):
        skipped = plan_titles(non_opf, {})
        assert skipped.pending == []
        assert "book_title_translated" not in skipped.manifest["meta"]
