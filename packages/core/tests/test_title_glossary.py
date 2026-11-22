"""EPUB navigation mappings reach the glossary before final Review."""

import json
import re
import zipfile

import pytest
from wenyi_core.config import Config
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.models.document import Chapter, Document, Segment
from wenyi_core.models.glossary import GlossaryTerm
from wenyi_core.pipeline.orchestrator import Orchestrator
from wenyi_core.storage.file import FileStorage
from wenyi_core.storage.sqlite import SqliteStorage

from tests.fake_llm import routing_handler
from tests.review_fixtures import fix_json, review_json
from tests.sample_data import write_nested_toc_epub


@pytest.fixture(params=[FileStorage, SqliteStorage])
def navigation_project(request, tmp_path):
    store = request.param(str(tmp_path / "state"))
    config = Config.from_dict(
        {"llm": {"preset": "fake"}, "language": {"source": "en", "target": "zh"}}
    )
    chapter = Chapter(
        index=0,
        title="The Copper Arch",
        segments=[
            Segment(
                index=0,
                source="The Copper Arch",
                target="铜拱门",
                kind="heading",
                anchor="tn0_0",
            ),
            Segment(index=1, source="A side trail.", target="一条小路。"),
        ],
    )
    entries = [
        {
            "entry_id": "nav:0",
            "title": "The Copper Arch",
            "segment_anchor": "tn0_0",
            "depth": 0,
        },
        {"entry_id": "nav:1", "title": "Side Trail", "depth": 1},
        {"entry_id": "ncx:0", "title": "The Copper Arch", "segment_anchor": "tn0_0"},
        {"entry_id": "ncx:1", "title": "Side Trail"},
        {"entry_id": "nav:2", "title": "Saved Title", "title_translated": "已存标题"},
    ]
    manifest = store.stage_document(
        Document(
            title="Book",
            source_lang="en",
            target_lang="zh",
            fmt="epub",
            chapters=[chapter],
            meta={"toc_entries": entries},
        ),
        source_hash="a" * 64,
    )
    manifest["chapters"][0]["toc_entry_id"] = "nav:0"
    store.save_manifest(manifest)
    try:
        yield config, store
    finally:
        store.close()


def _translate_navigation(config, store, client):
    Orchestrator(config, client)._translation._titles.run(store, store)


def _title_handler(messages, *args):
    titles = re.findall(r"^\[\d+\] (.*)$", messages[-1]["content"], re.MULTILINE)
    return json.dumps({"titles": ["支路" for _ in titles]}, ensure_ascii=False)


def test_all_navigation_mappings_are_stored_in_tree_order(navigation_project):
    config, store = navigation_project
    before = store.load_chapter(0)
    client = FakeClient(handler=_title_handler)

    _translate_navigation(config, store, client)

    assert [(term.source, term.target) for term in store.all_terms()] == [
        ("The Copper Arch", "铜拱门"),
        ("Side Trail", "支路"),
        ("Saved Title", "已存标题"),
    ]
    assert len(client.calls) == 1
    assert store.load_chapter(0).segments == before.segments
    assert store.load_manifest()["title"] == "Book"


def test_resume_backfills_saved_titles_without_model_calls(navigation_project):
    config, store = navigation_project
    manifest = store.load_manifest()
    for entry in manifest["meta"]["toc_entries"]:
        entry["title_translated"] = "已译 " + entry["title"]
    store.save_manifest(manifest)
    client = FakeClient()

    _translate_navigation(config, store, client)
    terms = store.all_terms()
    _translate_navigation(config, store, client)

    assert len(terms) == 3
    assert store.all_terms() == terms
    assert store.open_conflicts() == []
    assert client.calls == []


@pytest.mark.parametrize("existing_target", ["手定章名", "铜拱门"])
def test_navigation_conflicts_preserve_existing_terms_and_do_not_repeat(
    navigation_project, existing_target
):
    config, store = navigation_project
    original = GlossaryTerm(
        source="The Copper Arch",
        target=existing_target,
        aliases=["Copper Arch"],
        note="人工确认",
        type="custom",
        first_chapter=0,
    )
    store.upsert_term(original)
    _translate_navigation(config, store, FakeClient(handler=_title_handler))

    saved = store.get_term(original.source)
    assert saved is not None
    assert saved.target == original.target
    assert saved.aliases == original.aliases
    assert saved.note == original.note
    assert saved.type == original.type
    conflicts = store.open_conflicts()
    assert len(conflicts) == (existing_target != "铜拱门")
    if conflicts:
        assert conflicts[0]["proposed_target"] == "铜拱门"

    _translate_navigation(config, store, FakeClient())
    assert store.open_conflicts() == conflicts


def test_failed_glossary_write_recovers_from_saved_titles(navigation_project, monkeypatch):
    config, store = navigation_project
    upsert = store.upsert_term
    calls = 0

    def fail_second(term, chapter=None):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("interrupted glossary write")
        return upsert(term, chapter)

    monkeypatch.setattr(store, "upsert_term", fail_second)
    with pytest.raises(RuntimeError, match="interrupted glossary write"):
        _translate_navigation(config, store, FakeClient(handler=_title_handler))
    assert all(
        entry.get("title_translated") for entry in store.load_manifest()["meta"]["toc_entries"]
    )
    assert len(store.all_terms()) == 1

    monkeypatch.setattr(store, "upsert_term", upsert)
    resumed = FakeClient()
    _translate_navigation(config, store, resumed)

    assert len(store.all_terms()) == 3
    assert resumed.calls == []


def test_non_epub_title_translation_does_not_seed_navigation_terms(navigation_project):
    config, store = navigation_project
    manifest = store.load_manifest()
    manifest["fmt"] = "txt"
    store.save_manifest(manifest)

    _translate_navigation(config, store, FakeClient(handler=_title_handler))

    assert store.all_terms() == []


def test_navigation_batch_failure_only_retries_pending_titles(navigation_project):
    config, store = navigation_project
    manifest = store.load_manifest()
    manifest["meta"]["toc_entries"] = [
        {"entry_id": f"nav:{index}", "title": f"Title {index}"} for index in range(43)
    ]
    store.save_manifest(manifest)
    calls = 0

    def interrupt_second_batch(messages, *args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("interrupted title batch")
        return _title_handler(messages)

    with pytest.raises(RuntimeError, match="interrupted title batch"):
        _translate_navigation(config, store, FakeClient(handler=interrupt_second_batch))
    assert (
        len(
            [
                entry
                for entry in store.load_manifest()["meta"]["toc_entries"]
                if entry.get("title_translated")
            ]
        )
        == 40
    )

    resumed = FakeClient(handler=_title_handler)
    _translate_navigation(config, store, resumed)

    assert len(resumed.calls) == 1
    assert len(store.all_terms()) == 43


def test_different_navigation_translations_record_one_conflict(navigation_project):
    config, store = navigation_project
    manifest = store.load_manifest()
    manifest["meta"]["toc_entries"][2]["title_translated"] = "另一种拱门"
    store.save_manifest(manifest)

    _translate_navigation(config, store, FakeClient(handler=_title_handler))
    _translate_navigation(config, store, FakeClient())

    assert store.get_term("The Copper Arch").target == "铜拱门"
    conflicts = store.open_conflicts()
    assert len(conflicts) == 1
    assert conflicts[0]["existing_target"] == "铜拱门"
    assert conflicts[0]["proposed_target"] == "另一种拱门"


@pytest.mark.parametrize("toc_kind", ["nav", "ncx", "both"])
@pytest.mark.parametrize("review,autofix", [(False, True), (True, False), (True, True)])
def test_navigation_terms_feed_review_and_only_autofix_changes_body(
    tmp_path, toc_kind, review, autofix
):
    source = tmp_path / "book.epub"
    write_nested_toc_epub(str(source), toc_kind=toc_kind)
    with zipfile.ZipFile(source) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    # A chapter-number prefix prevents heading reuse; the visible TOC is ordinary body text.
    files["OEBPS/body.xhtml"] = (
        files["OEBPS/body.xhtml"]
        .replace(b">Section 1</h2>", b">CHAPTER ISection 1</h2>")
        .replace(b"<body>", b'<body><p><a href="#section-1">Section 1</a></p>')
    )
    with zipfile.ZipFile(source, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    config = Config.from_dict(
        {
            "llm": {"preset": "fake"},
            "language": {"source": "en", "target": "zh"},
            "paths": {"state_dir": str(tmp_path / "state")},
            "pipeline": {
                "book_understanding": False,
                "polish": False,
                "annotation_alignment": False,
                "review": review,
                "review_autofix": autofix,
                "review_agent_loop": False,
                "review_conflict_arbitration": False,
                "review_concurrency": 1,
            },
            "output": {"bilingual": True},
        }
    )
    reviewed = []

    def handler(messages, tier, json_mode):
        system = messages[0]["content"]
        user = messages[-1]["content"]
        if "chapter title translator" in system:
            titles = re.findall(r"^\[\d+\] (.*)$", user, re.MULTILINE)
            return json.dumps(
                {"titles": [title.replace("Section 1", "第一节") for title in titles]}
            )
        if "literary translator" in system:
            sources = re.findall(r"^\[\d+\] (.*)$", user, re.MULTILINE)
            return json.dumps(
                {"translations": [text.replace("Section 1", "旧节名") for text in sources]}
            )
        if "terminology" in system and "extractor" in system:
            return '{"terms": []}'
        if "translation reviewer" in system:
            reviewed.append(user)
            assert "Section 1 → 第一节" in user
            pairs = re.findall(
                r"^\[(\d+)\] Source:.*?\n    Translation: ([^\n]*)", user, re.MULTILINE
            )
            return review_json(
                user,
                [
                    {
                        "index": int(index),
                        "type": "terminology",
                        "detail": "章名与术语表不一致",
                        "suggestion": target.replace("旧节名", "第一节"),
                    }
                    for index, target in pairs
                    if "旧节名" in target
                ],
            )
        if "cautious revision editor" in system:
            target = user.split("Complete current Simplified Chinese translation]\n", 1)[1]
            target = target.split("\n\nMake only the minimum edits", 1)[0].strip()
            return fix_json(user, target.replace("旧节名", "第一节"))
        return routing_handler(messages, tier, json_mode)

    result = Orchestrator(config, FakeClient(handler=handler)).run_all(
        str(source), out_path=str(tmp_path / "translated.epub")
    )
    store = result["store"]
    try:
        assert store.get_term("Section 1").target == "第一节"
        assert len(store.all_terms()) == 4
        assert bool(reviewed) == review
        targets = "\n".join(
            segment.target or ""
            for row in store.load_manifest()["chapters"]
            for segment in store.load_chapter(row["index"]).text_segments
        )
        assert ("旧节名" not in targets) == (review and autofix)
        expected_title = "第一节" if review and autofix else "旧节名"
        for row in store.load_manifest()["chapters"]:
            for segment in store.load_chapter(row["index"]).text_segments:
                assert segment.target == segment.source.replace("Section 1", expected_title)
        for output in result["outputs"]:
            with zipfile.ZipFile(output) as archive:
                body = archive.read("OEBPS/body.xhtml").decode()
                assert ("旧节名" not in body) == (review and autofix)
        if review:
            assert result["review_result"]["summary"]["review_round_count"] == 3
    finally:
        store.close()
