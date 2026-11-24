"""``pipeline.batch_glossary``: full versus per-batch relevant glossary entries."""

import json
import re

import pytest
from pydantic import ValidationError
from wenyi_core.config import Config
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.models.document import Segment
from wenyi_core.models.glossary import GlossaryTerm
from wenyi_core.pipeline.orchestrator import Orchestrator
from wenyi_core.pipeline.translate.translation import POLISH_PENDING
from wenyi_core.storage.run_store import STATUS_PENDING

from tests.fake_llm import routing_handler
from tests.precision_fixtures import PrecisionHandler

# Every entry recurs: extraction only shows existing entries seen at least twice in the book.
BOOK = (
    "Alice waited by the window.\n\nThe Doctor opened the door.\n\nA letter arrived later."
    "\n\nAlice asked the Doctor about the letter."
)
GLOSSARY_LINE = re.compile(r"^- (\S+) → ", re.MULTILINE)


@pytest.fixture
def config(tmp_path):
    return Config.from_dict(
        {
            "language": {"source": "en", "target": "zh"},
            "llm": {"preset": "fake"},
            "paths": {"state_dir": str(tmp_path / "state")},
            "segment": {"max_tokens_per_batch": 1, "max_tokens_per_segment": 0},
            "pipeline": {
                "review": False,
                "polish": False,
                "book_understanding": False,
                "annotation_alignment": False,
                "align_retry_limit": 1,
            },
        }
    )


def _glossary_block(text: str, heading: str) -> list[str]:
    block = text.split(heading, 1)[1].split("\n\n", 1)[0]
    return GLOSSARY_LINE.findall(block)


def _tagged_glossary_block(system: str) -> list[str]:
    block = system.split("<glossary_data>\n", 1)[1].split("\n</glossary_data>", 1)[0]
    return GLOSSARY_LINE.findall(block)


def _batch_events(store):
    with open(store.event_log_path, encoding="utf-8") as stream:
        events = [json.loads(line) for line in stream if line.strip()]
    return [event for event in events if event["event"] == "batch_translated"]


def _handler(messages, tier, json_mode):
    system = messages[0]["content"]
    user = messages[-1]["content"]
    if "terminology" in system and "extractor" in system:
        return json.dumps({"terms": []})
    if "Polish the translations from your previous JSON response" in user:
        # Force the standalone polisher fallback so its glossary is observable.
        return json.dumps({"polished": []})
    return routing_handler(messages, tier, json_mode)


def _prepare(config, tmp_path, client, extra_terms=()):
    source = tmp_path / "book.txt"
    source.write_text(BOOK, encoding="utf-8")
    orchestrator = Orchestrator(config, client=client)
    store = orchestrator.prepare(str(source))
    store.upsert_term(GlossaryTerm(source="Zebra", target="斑马"))
    store.upsert_term(GlossaryTerm(source="Alice", target="爱丽丝", type="person"))
    store.upsert_term(GlossaryTerm(source="Physician", target="医生", aliases=["Doctor"]))
    store.upsert_term(GlossaryTerm(source="letter", target="信"))
    for term in extra_terms:
        store.upsert_term(term)
    return orchestrator, store, source


def test_default_is_all_and_unknown_scopes_are_rejected():
    assert Config.from_dict({}).pipeline.batch_glossary == "all"
    relevant = Config.from_dict({"pipeline": {"batch_glossary": "relevant"}})
    assert relevant.pipeline.batch_glossary == "relevant"
    with pytest.raises(ValidationError, match="batch_glossary"):
        Config.from_dict({"pipeline": {"batch_glossary": "chapter"}})


def test_all_scope_sends_every_entry_in_insertion_order(tmp_path, config):
    client = FakeClient(handler=_handler)
    orchestrator, store, source = _prepare(config, tmp_path, client)
    orchestrator.run(str(source))
    calls = [c for c in client.calls if c["operation"] == "translation.body"]
    assert len(calls) == 4
    for call in calls:
        system = call["messages"][0]["content"]
        user = call["messages"][-1]["content"]
        assert _tagged_glossary_block(system) == [
            "Zebra",
            "Alice",
            "Physician",
            "letter",
        ]
        # Book-stable style, synopsis and glossary live in system ahead of dynamic user context.
        assert system.index("<style_data>") < system.index("<synopsis_data>")
        assert system.index("<synopsis_data>") < system.index("<glossary_data>")
        assert "[Chapter digest]" in user
    events = _batch_events(store)
    assert [e["glossary_terms"] for e in events] == [4, 4, 4, 4]
    assert all(e["glossary_tokens"] > 0 for e in events)


@pytest.mark.parametrize("polish_concurrency", [1, 2])
def test_relevant_scope_filters_translation_polish_and_extraction(
    tmp_path, config, polish_concurrency
):
    config.pipeline.batch_glossary = "relevant"
    config.pipeline.polish = True
    config.pipeline.polish_concurrency = polish_concurrency
    client = FakeClient(handler=_handler)
    orchestrator, store, source = _prepare(config, tmp_path, client)
    orchestrator.run(str(source))

    def blocks(operation, heading):
        return [
            _glossary_block(c["messages"][-1]["content"], heading)
            for c in client.calls
            if c["operation"] == operation and heading in c["messages"][-1]["content"]
        ]

    # Each batch also sees the following-source reference.
    full = ["Alice", "Physician", "letter"]
    expected = [["Alice", "Physician"], ["Physician", "letter"], full, full]
    trans_blocks = [
        _tagged_glossary_block(c["messages"][0]["content"])
        for c in client.calls
        if c["operation"] == "translation.body"
        and "<glossary_data>\n" in c["messages"][0]["content"]
    ]
    assert trans_blocks == expected
    polish = blocks("polish.body", "[Glossary]\n")
    assert sorted(polish) == sorted(expected)
    # Extraction sees only entries occurring in the extracted batch (chapter fallback last).
    extract = blocks(
        "glossary.extract", "[Existing glossary (reference; do not repeat unchanged entries)]\n"
    )
    assert extract == [["Alice"], ["Physician"], ["letter"], full, full]
    events = _batch_events(store)
    assert [e["glossary_terms"] for e in events] == [2, 2, 3, 3]


def test_relevant_scope_matches_digest_and_annotation_contexts(tmp_path, config):
    config.pipeline.batch_glossary = "relevant"
    orchestrator = Orchestrator(config, client=FakeClient(handler=_handler))
    snapshot = [
        GlossaryTerm(source="Alice", target="爱丽丝"),
        GlossaryTerm(source="Bob", target="鲍勃"),
        GlossaryTerm(source="Carol", target="卡罗尔"),
        GlossaryTerm(source="Dave", target="戴夫"),
        GlossaryTerm(source="Eve", target="伊芙"),
    ]
    terms = orchestrator._translation.batch_terms(
        snapshot,
        [Segment(index=0, source="She smiled at Alice.")],
        chapter_digest="Bob returns home.",
        annotation_contexts=[[{"target_key": "n1", "source": "Carol's note"}]],
        next_source="Dave knocked.",
    )
    assert [t.source for t in terms] == ["Alice", "Bob", "Carol", "Dave"]
    config.pipeline.batch_glossary = "all"
    assert (
        orchestrator._translation.batch_terms(
            snapshot,
            [Segment(index=0, source="nothing")],
            chapter_digest="",
            annotation_contexts=[[]],
            next_source="",
        )
        is snapshot
    )


def test_relevant_scope_bounds_prompt_size_for_large_glossaries(tmp_path, config):
    config.pipeline.batch_glossary = "relevant"
    filler = [GlossaryTerm(source=f"Term{i:04d}x", target=f"术语{i}") for i in range(3000)]
    client = FakeClient(handler=_handler)
    orchestrator, store, source = _prepare(config, tmp_path, client, filler)
    orchestrator.run(str(source))
    for call in client.calls:
        if call["operation"] in {"translation.body", "glossary.extract"}:
            assert all("Term0" not in message["content"] for message in call["messages"])
    events = _batch_events(store)
    assert max(e["glossary_terms"] for e in events) == 3
    assert max(e["glossary_tokens"] for e in events) < 100


def test_relevant_scope_filters_resumed_polish_of_saved_drafts(tmp_path, config):
    config.pipeline.batch_glossary = "relevant"
    orchestrator, store, source = _prepare(config, tmp_path, FakeClient(handler=_handler))
    orchestrator.run(str(source))
    chapter = store.load_chapter(0)
    for segment in chapter.text_segments:
        segment.target_before_polish = segment.target
    # A saved draft whose polish was interrupted; resume repolishes it from saved text.
    chapter.text_segments[1].meta[POLISH_PENDING] = True
    store.save_chapter(chapter)
    store.set_chapter_status(0, STATUS_PENDING)

    config.pipeline.polish = True
    client = FakeClient(handler=_handler)
    Orchestrator(config, client=client).run(str(source))
    polish = [c for c in client.calls if c["operation"] == "polish.body"]
    assert len(polish) == 1
    user = polish[0]["messages"][-1]["content"]
    assert _glossary_block(user, "[Glossary]\n") == ["Physician", "letter"]
    assert not [c for c in client.calls if c["operation"] == "translation.body"]


def test_relevant_scope_filters_precision_drafts_and_synthesis(tmp_path, config):
    config.pipeline.batch_glossary = "relevant"
    config.pipeline.polish = True
    config.pipeline.translation_mode = "three_draft_synthesis"
    handler = PrecisionHandler()

    def route(messages, tier, json_mode):
        if any("Task (JSON):\n" in message["content"] for message in messages):
            return handler(messages, tier, json_mode)
        return _handler(messages, tier, json_mode)

    client = FakeClient(handler=route)
    orchestrator, _store, source = _prepare(config, tmp_path, client)
    orchestrator.run(str(source))
    packets = [
        json.loads(c["messages"][1]["content"].split("\n", 1)[1])
        for c in client.calls
        if c["operation"] in {"translation.body", "polish.body"}
    ]
    full = ["Alice", "Physician", "letter"]
    expected = [["Alice", "Physician"], ["Physician", "letter"], full, full]
    # Three drafts plus one synthesis per batch share the batch's filtered glossary.
    assert [GLOSSARY_LINE.findall(p["glossary"]) for p in packets] == [
        terms for terms in expected for _ in range(4)
    ]
