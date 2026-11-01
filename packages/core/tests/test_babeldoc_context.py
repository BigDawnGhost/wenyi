"""BabelDOC prose context must not consume layout-only translation units."""

import json
import re

import pytest
from wenyi_core.config import Config
from wenyi_core.ingest.models import Chapter, Document, Segment
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.pipeline.babeldoc_context import BabeldocContext
from wenyi_core.pipeline.orchestrator import Orchestrator
from wenyi_core.pipeline.translation import TranslationService
from wenyi_core.storage.file import FileStorage
from wenyi_core.storage.sqlite import SqliteStorage

from tests.fake_llm import routing_handler
from tests.precision_fixtures import PrecisionHandler


@pytest.fixture(params=[FileStorage, SqliteStorage])
def store(request, tmp_path):
    storage = request.param(str(tmp_path / "run"))
    yield storage
    storage.close()


def _segment(index, source, label):
    return Segment(
        index=index,
        source=source,
        kind="heading" if label == "title" else "text",
        meta={"babeldoc_id": f"0:{index}", "layout_label": label},
    )


def _initialize(store, segments):
    document = Document(
        title="Study",
        source_lang="en",
        target_lang="zh",
        source_path="synthetic.pdf",
        fmt="pdf",
        meta={"babeldoc": True},
        chapters=[Chapter(index=0, segments=segments, meta={"babeldoc": True})],
    )
    manifest = store.stage_document(document, source_hash="0" * 64)
    manifest["initialized"] = True
    store.save_manifest(manifest)


def _config():
    return Config.from_dict(
        {
            "language": {"source": "en", "target": "zh"},
            "llm": {"preset": "fake"},
            "segment": {"max_tokens_per_batch": 1000},
            "pipeline": {
                "polish": False,
                "review": False,
                "book_understanding": False,
                "annotation_alignment": False,
            },
        }
    )


def _record_plans(orchestrator, monkeypatch):
    executor = (
        orchestrator._translation._precision
        if orchestrator.config.pipeline.translation_mode == "best_of_three"
        else orchestrator._translation._batches
    )
    plans = []
    execute = executor.execute

    def record(plan, *args, **kwargs):
        plans.append(plan)
        return execute(plan, *args, **kwargs)

    monkeypatch.setattr(executor, "execute", record)
    return plans


@pytest.mark.parametrize(
    "mode,polish", [("standard", False), ("standard", True), ("best_of_three", True)]
)
def test_body_context_skips_layout_units_without_dropping_them(store, monkeypatch, mode, polish):
    segments = [
        _segment(10, "First body paragraph.", "plain text"),
        _segment(20, "Journal footer", "abandon"),
        _segment(30, "Table cell", "fallback_line"),
        _segment(40, "Table caption", "table_caption"),
        _segment(50, "Second body paragraph.", "plain text"),
    ]
    _initialize(store, segments)
    chapter = store.load_chapter(0)
    chapter.meta["source_digest"] = "Chapter guidance"
    store.save_chapter(chapter)
    config = _config()
    config.pipeline.translation_mode = mode
    config.pipeline.polish = polish
    handler = PrecisionHandler() if mode == "best_of_three" else routing_handler
    orchestrator = Orchestrator(config, client=FakeClient(handler=handler))
    plans = _record_plans(orchestrator, monkeypatch)
    orchestrator._translation.run(store, book_synopsis="Book guidance")

    assert [plan.sources for plan in plans] == [(segment.source,) for segment in segments]
    assert plans[0].next_source == segments[-1].source
    assert plans[-1].context == store.load_chapter(0).segments[0].target
    assert plans[-1].next_source == ""
    assert all(plan.context == plan.next_source == "" for plan in plans[1:-1])
    assert all(plan.book_synopsis == plan.chapter_digest == "" for plan in plans[1:-1])
    assert plans[0].book_synopsis == plans[-1].book_synopsis == "Book guidance"
    assert plans[0].chapter_digest == plans[-1].chapter_digest == "Chapter guidance"
    saved = store.load_chapter(0)
    assert [(s.index, s.source, s.meta) for s in saved.segments] == [
        (s.index, s.source, s.meta) for s in segments
    ]
    assert all(segment.target is not None for segment in saved.segments)
    assert store.read_artifact("context.json") is None


def test_policy_uses_heading_ranges_not_whole_toc_pages():
    segments = [
        _segment(0, "Article title", "title"),
        _segment(1, "Author and institution", "plain text"),
        _segment(2, "ABSTRACT", "title"),
        _segment(3, "Abstract prose.", "plain text"),
        _segment(4, "1 | Introduction", "title"),
        _segment(5, "First body paragraph.", "plain text"),
        _segment(6, "Page footer", "abandon"),
        _segment(7, "Second body paragraph.", "plain text"),
        _segment(8, "References", "title"),
        _segment(9, "A bibliography entry.", "plain text"),
        _segment(10, "Funding", "title"),
        _segment(11, "Grant information.", "plain text"),
        _segment(12, "2 | Discussion", "title"),
        _segment(13, "Discussion prose.", "plain text"),
        _segment(14, "Unclassified text", None),
    ]
    chapter = Chapter(index=0, title="Study", segments=segments, meta={"babeldoc": True})
    before = chapter.model_dump()
    policy = BabeldocContext.for_chapter(chapter)
    assert policy is not None
    assert [i for i, body in enumerate(policy.body) if body] == [3, 5, 7, 13]
    assert policy.next_source(segments, 4) == ""  # Do not cross the introduction heading.
    assert policy.next_source(segments, 6) == segments[7].source
    assert policy.next_source(segments, 8) == ""  # References are not following prose.
    assert chapter.model_dump() == before

    # TOC splitting is page-based: the conclusion can end above References on that page.
    chapter = Chapter(
        index=1,
        title="Translated chapter title",
        segments=segments[7:10],
        meta={"babeldoc": True, "pdf_toc_title": "References"},
    )
    policy = BabeldocContext.for_chapter(chapter)
    assert policy is not None
    assert policy.body == (True, False, False)
    chapter.segments = segments[9:10]  # A references-only continuation has no heading.
    policy = BabeldocContext.for_chapter(chapter)
    assert policy is not None
    assert policy.body == (False,)


def test_layout_labels_alone_do_not_activate_pdf_policy():
    chapter = Chapter(
        index=0,
        segments=[Segment(index=0, source="HTML text", meta={"layout_label": "plain text"})],
    )
    assert BabeldocContext.for_chapter(chapter) is None


@pytest.mark.parametrize(
    "checkpoints,start,count,covered",
    [
        ({"0:5"}, 1, 2, True),
        ({"0:1", "1:2"}, 0, 3, True),
        ({"0:1", "2:1"}, 0, 3, False),
        ({"0:2"}, 2, 1, False),
        ({"invalid", "-1:5", "0:0"}, 0, 1, False),
    ],
)
def test_glossary_checkpoint_coverage_has_no_gaps(checkpoints, start, count, covered):
    assert TranslationService._glossary_checkpoint_covers(checkpoints, start, count) is covered


@pytest.mark.parametrize("budget", [1, 1000])
def test_resume_discards_legacy_context_and_reuses_old_larger_glossary_checkpoint(
    store, monkeypatch, budget
):
    segments = [
        _segment(10, "Saved body.", "plain text"),
        _segment(20, "Saved footer", "abandon"),
        _segment(30, "42", "fallback_line"),
        _segment(40, "Pending body.", "plain text"),
        _segment(50, "Pending footer", "abandon"),
        _segment(60, "Final body.", "plain text"),
    ]
    for segment, target in zip(segments, ["SAVED BODY", "SAVED FOOTER", ""]):
        segment.target = target
    _initialize(store, segments)
    store.write_artifact("context.json", {"recent_targets": ["LEGACY TABLE NOISE", "FUTURE TEXT"]})
    # Before role isolation, one mixed batch covered all three completed units.
    store.log_event("batch_glossary_extracted", chapter=0, start_index=0, count=3, summary={})
    config = _config()
    config.segment.max_tokens_per_batch = budget
    orchestrator = Orchestrator(config, client=FakeClient(handler=routing_handler))
    plans = _record_plans(orchestrator, monkeypatch)
    extracted = []
    extract = orchestrator._translation.extract_batch_glossary

    def record_extraction(*args, **kwargs):
        extracted.append(args[3])
        return extract(*args, **kwargs)

    monkeypatch.setattr(orchestrator._translation, "extract_batch_glossary", record_extraction)
    assert orchestrator._translation.progress_counts(store, [0]) == (6, 3)
    orchestrator._translation.run(store, book_synopsis="")
    assert [plan.segment_indices for plan in plans] == [(40,), (50,), (60,)]
    assert plans[0].context == "SAVED BODY"
    assert plans[0].next_source == segments[-1].source
    assert plans[1].context == plans[1].next_source == ""
    assert plans[-1].context == "SAVED BODY\n" + store.load_chapter(0).segments[3].target
    assert extracted == [3, 4, 5]
    assert store.load_chapter(0).segments[:3] == segments[:3]
    assert orchestrator._translation.progress_counts(store, [0]) == (6, 6)


def test_interrupted_run_rebuilds_context_without_retranslating_saved_units(store, monkeypatch):
    segments = [
        _segment(0, "First body.", "plain text"),
        _segment(1, "Page footer", "abandon"),
        _segment(2, "Second body.", "plain text"),
    ]
    _initialize(store, segments)
    store.write_artifact("context.json", {"recent_targets": ["LEGACY NOISE"]})

    def interrupt(messages, tier, json_mode):
        if "literary translator" in messages[0]["content"]:
            if "[0] Second body." in messages[-1]["content"]:
                raise RuntimeError("simulated interruption")
        return routing_handler(messages, tier, json_mode)

    config = _config()
    first = Orchestrator(config, client=FakeClient(handler=interrupt))
    with pytest.raises(RuntimeError, match="simulated interruption"):
        first._translation.run(store, book_synopsis="")
    saved = store.load_chapter(0).segments
    assert all(segment.target is not None for segment in saved[:2])
    assert saved[2].target is None
    config.segment.max_tokens_per_batch = 1
    resumed = Orchestrator(config, client=FakeClient(handler=routing_handler))
    plans = _record_plans(resumed, monkeypatch)
    resumed._translation.run(store, book_synopsis="")
    assert len(plans) == 1
    assert plans[0].sources == ("Second body.",)
    assert plans[0].context == saved[0].target
    assert store.load_chapter(0).segments[:2] == saved[:2]


@pytest.mark.parametrize("only_chapter", [None, 1])
def test_completed_previous_chapter_does_not_supply_pdf_context(store, monkeypatch, only_chapter):
    segments = [_segment(0, "Previous chapter.", "plain text")]
    segments[0].target = "PREVIOUS CHAPTER TARGET"
    _initialize(store, segments)
    store.set_chapter_status(0, "done")
    store.write_artifact(
        "context.json", {"recent_targets": ["PREVIOUS CHAPTER TARGET", "OLD FOOTER"]}
    )
    store.save_chapter(
        Chapter(
            index=1,
            segments=[_segment(1, "New chapter.", "plain text")],
            meta={"babeldoc": True},
        )
    )
    manifest = store.load_manifest()
    manifest["chapters"].append({"index": 1, "title": "Second", "status": "pending"})
    store.save_manifest(manifest)
    orchestrator = Orchestrator(_config(), client=FakeClient(handler=routing_handler))
    plans = _record_plans(orchestrator, monkeypatch)
    orchestrator._translation.run(store, book_synopsis="", only_chapter=only_chapter)
    assert len(plans) == 1
    assert plans[0].context == plans[0].next_source == ""


def test_section_boundaries_reset_context_even_when_old_targets_exist(store, monkeypatch):
    segments = [
        _segment(0, "Earlier body.", "plain text"),
        _segment(1, "2 | Methods", "title"),
        _segment(2, "Current body.", "plain text"),
        _segment(3, "References", "title"),
        _segment(4, "Reference entry.", "plain text"),
    ]
    segments[0].target = "OLD SECTION"
    segments[1].target = "SAVED HEADING"
    _initialize(store, segments)
    orchestrator = Orchestrator(_config(), client=FakeClient(handler=routing_handler))
    plans = _record_plans(orchestrator, monkeypatch)
    orchestrator._translation.run(store, book_synopsis="")
    assert all(plan.context == plan.next_source == "" for plan in plans)
    assert store.read_artifact("context.json") is None


def test_alignment_fallback_and_polish_cannot_reintroduce_layout_neighbors(store):
    segments = [
        _segment(0, "First body.", "plain text"),
        _segment(1, "Second body.", "plain text"),
        _segment(2, "FOOTER NOISE", "abandon"),
        _segment(3, "Third body.", "plain text"),
    ]
    _initialize(store, segments)
    references = []

    def handler(messages, tier, json_mode):
        user = messages[-1]["content"]
        if "literary translator" in messages[0]["content"]:
            sources = re.findall(r"^\[\d+\] (.*)$", user, flags=re.MULTILINE)
            if "Polish the translations from your previous JSON response" not in user:
                reference = user.split(
                    "[Following source paragraph] (reference only; do not translate)\n", 1
                )[1].split("\n\n", 1)[0]
                references.append((sources, reference))
                if len(sources) > 1:
                    return '{"translations":[]}'  # Force singleton alignment recovery.
        return routing_handler(messages, tier, json_mode)

    config = _config()
    config.pipeline.polish = True
    config.pipeline.align_retry_limit = 0
    client = FakeClient(handler=handler)
    orchestrator = Orchestrator(config, client=client)
    orchestrator._translation.run(store, book_synopsis="")
    assert references[:3] == [
        (["First body.", "Second body."], json.dumps("Third body.")),
        (["First body."], json.dumps("Second body.")),
        (["Second body."], json.dumps("Third body.")),
    ]
    assert references[-1] == (["Third body."], "(none)")
    polish_calls = [call for call in client.calls if call["operation"] == "polish.body"]
    assert len(polish_calls) == 3
    assert all(
        "FOOTER NOISE" not in message["content"]
        for call in (polish_calls[0], polish_calls[-1])
        for message in call["messages"]
    )
