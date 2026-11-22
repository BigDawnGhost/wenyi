"""Exercise the CLI note mode with synthetic passages, including publication and resume."""

import json
from threading import Barrier, get_ident

import pytest
from wenyi_core.agents.glossary_note import GlossaryNoteAgent, NoteResult, NoteWindow
from wenyi_core.agents.translator import Translator
from wenyi_core.config import Config
from wenyi_core.glossary.notes import guidance
from wenyi_core.llm.operations import configured_operations
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.models.document import Segment
from wenyi_core.models.glossary import GlossaryTerm
from wenyi_core.pipeline.orchestrator import Orchestrator
from wenyi_core.pipeline.translate.glossary_notes import GlossaryNotes
from wenyi_core.pipeline.translate.precision import _PrecisionRun
from wenyi_core.pipeline.translate.translation import ChapterTranslationError
from wenyi_core.pipeline.translate.translation_batch import BatchPlan

from tests.fake_llm import routing_handler
from tests.glossary_fixtures import book_store, inputs
from tests.storage_fixtures import require_value


def config(workers=2):
    return Config.from_dict(
        {
            "language": {"source": "en", "target": "zh"},
            "llm": {"preset": "fake"},
            "pipeline": {"glossary_note_workers": workers},
        }
    )


def update(ref, text="用于称呼"):
    return json.dumps({"updates": [{"text": text, "refs": [ref]}]})


def test_unusable_response_is_checkpointed_without_losing_future_evidence(tmp_path):
    store, _ = book_store(tmp_path)
    store.upsert_term(GlossaryTerm("May", "梅", note="editor"))
    client = FakeClient(lambda *_: "not JSON")
    service = GlossaryNotes(GlossaryNoteAgent(client, config()), store)
    segments = [Segment(index=0, source="May arrived.", target="梅到了。")]
    service.maintain(0, 0, segments)
    assert len(client.calls) == 2
    (key,) = [k for k in store.list_artifacts("glossary/notes/") if not k.endswith("/done.json")]
    record = require_value(store.read_artifact(key))
    assert record["outcome"] == "not_adopted" and record["rejections"]
    assert record["lines"] == [] and require_value(store.get_term("May")).note == "editor"
    # Simulate a stop after recording the outcome but before the batch marker.
    store.delete_artifact(key.rsplit("/", 1)[0] + "/done.json")
    service.maintain(0, 0, segments)
    assert len(client.calls) == 2
    service.maintain(0, 1, [Segment(index=1, source="May returned.", target="梅回来了。")])
    assert len(client.calls) == 4


def test_provider_failure_during_repair_leaves_the_term_retryable(tmp_path):
    store, _ = book_store(tmp_path)
    store.upsert_term(GlossaryTerm("May", "梅", note="editor"))
    segments = [Segment(index=0, source="May arrived.", target="梅到了。")]

    def fail_on_repair(messages, *_):
        if len(messages) == 2:
            return "not JSON"
        raise ValueError("provider credentials")

    client = FakeClient(fail_on_repair)
    service = GlossaryNotes(GlossaryNoteAgent(client, config()), store)
    with pytest.raises(ValueError, match="provider credentials"):
        service.maintain(0, 0, segments)
    assert store.list_artifacts("glossary/notes/") == []
    client.handler = lambda *_: '{"updates":[]}'
    service.maintain(0, 0, segments)
    assert len(client.calls) == 3
    (key,) = [k for k in store.list_artifacts("glossary/notes/") if not k.endswith("/done.json")]
    assert require_value(store.read_artifact(key))["outcome"] == "no_update"


def test_parallel_workers_publish_on_caller_only_and_resume_prepared(tmp_path, monkeypatch):
    store, chapters = book_store(tmp_path)
    caller = get_ident()
    barrier = Barrier(2)
    for term in [GlossaryTerm("Alice", "艾丽丝", note="manual"), GlossaryTerm("Carl", "卡尔")]:
        store.upsert_term(term)

    def handler(messages, tier, json_mode):
        packet = json.loads(messages[-1]["content"])
        barrier.wait(timeout=5)
        return update(packet["first_evidence"]["ref"])

    client = FakeClient(handler)
    service = GlossaryNotes(GlossaryNoteAgent(client, config()), store)
    publish = store.upsert_term

    def interrupted(term, chapter=None):
        assert get_ident() == caller
        publish(term, chapter=chapter)
        if term.source == "Carl":
            raise RuntimeError("crash after append")
        return "unchanged"

    monkeypatch.setattr(store, "upsert_term", interrupted)
    with pytest.raises(RuntimeError, match="after append"):
        service.maintain(0, 0, chapters[0].text_segments)
    monkeypatch.setattr(store, "upsert_term", publish)
    assert service.maintain(0, 0, chapters[0].text_segments) == {"Alice", "Carl"}
    service.maintain(0, 0, chapters[0].text_segments)
    assert len(client.calls) == 2
    assert require_value(store.get_term("Alice")).note.startswith("manual\n[fact]")
    assert require_value(store.get_term("Carl")).note.count("用于称呼") == 1
    service.admit(GlossaryTerm("Alice", "艾丽丝", note="extractor overwrite"), 0)
    service.admit(GlossaryTerm("Alice", "另一译名"), 0)
    service.admit(GlossaryTerm("Alice", "另一译名"), 0)
    assert require_value(store.get_term("Alice")).note.startswith("manual\n")
    assert require_value(store.get_term("Alice")).target == "艾丽丝"
    assert len(store.open_conflicts()) == 1


def test_read_specific_context_and_note_page_without_whole_note_in_prompt():
    term = GlossaryTerm("May", "梅", note="EDITOR\n[rationale c0:s0:0-2] SECRET")
    window = NoteWindow(2, 7, (("May arrives.", "译文"), ("She is a doctor.", "她是医生。")))

    def handler(messages, tier, json_mode):
        if len(messages) == 2:
            assert "SECRET" not in messages[-1]["content"]
            assert "She is a doctor" not in messages[-1]["content"]
            return json.dumps(
                {
                    "reads": [
                        {"tool": "read_context", "index": 8, "offset": 7},
                        {"tool": "read_note", "section": "rationale"},
                    ]
                }
            )
        context, page = json.loads(messages[-1]["content"])["results"]
        assert context["source"] == "a doctor."
        assert "SECRET" in page["text"] and "EDITOR" not in page["text"]
        return update(context["ref"])

    result = GlossaryNoteAgent(FakeClient(handler), config()).update(term, window, [7])
    assert len(result.lines) == 1 and "c2:s8:7-16" in result.lines[0]
    assert guidance(result.lines[0]) == "用于称呼"


@pytest.mark.parametrize(
    "reply,limit",
    [
        (update("c4:s0:0-9"), 1),
        ('{"reads":[{"tool":"read_context","index":true}]}', 1),
        ('{"reads":[{"tool":"read_context","index":7}]}', 1),
        ('{"reads":[{"tool":"read_note","section":"all"}]}', 1),
        ('{"reads":[{"tool":"read_note","section":"guidance"}]}', 4),
    ],
)
def test_bad_reads_and_unseen_citations_cannot_publish(reply, limit):
    client = FakeClient(lambda *_: reply)
    result = GlossaryNoteAgent(client, config()).update(
        GlossaryTerm("May", "梅"), NoteWindow(0, 0, (("May", "梅"),)), [0]
    )
    assert result.outcome == "not_adopted" and not result.lines
    assert len(client.calls) == limit


def test_translation_reads_only_local_section_on_demand():
    note = (
        "Editor\n[guidance c0:s1:0-5] OLD\n[rationale c0:s2:0-5] REASON\n"
        "[guidance c0:s3:0-5] CURRENT"
    )
    terms = [GlossaryTerm("May", "梅", note=note), GlossaryTerm("Bob", "鲍勃", note="UNRELATED")]
    assert guidance(note) == "Editor\n[guidance c0:s3:0-5] CURRENT"

    def handler(messages, tier, json_mode):
        if len(messages) == 2:
            prompt = messages[1]["content"]
            assert "CURRENT" in prompt
            assert not any(word in prompt for word in ("REASON", "OLD", "UNRELATED"))
            return '{"read_note":{"term":"May","section":"rationale"}}'
        assert "REASON" in messages[-1]["content"] and "CURRENT" not in messages[-1]["content"]
        return '{"translations":["梅来了。"]}'

    client = FakeClient(handler)
    assert Translator(client, config()).translate_batch(["May arrives."], glossary_terms=terms) == [
        "梅来了。"
    ]
    assert len(client.calls) == 2


def test_incremental_pipeline_resumes_after_note_failure_and_uses_guidance(tmp_path):
    source, selected, store = inputs(tmp_path)
    selected.pipeline.glossary_note_workers = 2
    fail = True
    body = []

    def handler(messages, tier, json_mode):
        nonlocal fail
        system = messages[0]["content"]
        if system.startswith("Maintain one glossary note"):
            if fail:
                fail = False
                raise RuntimeError("interrupted note")
            packet = json.loads(messages[-1]["content"])
            return update(packet["first_evidence"]["ref"], text="LOCAL GUIDANCE")
        if "terminology" in system and "extractor" in system:
            return '{"terms":[{"source":"Alice","target":"艾丽丝","type":"term"}]}'
        if "literary translator" in system:
            body.append(messages[-1]["content"])
        return routing_handler(messages, tier, json_mode)

    client = FakeClient(handler)

    def run():
        Orchestrator(selected, client=client, storage=store, allow_glossary_notes=True).run(
            str(source)
        )

    with pytest.raises(ChapterTranslationError, match="interrupted note") as caught:
        run()
    cause = caught.value.__cause__
    assert isinstance(cause, RuntimeError) and str(cause) == "interrupted note"
    assert store.completed_batch_glossary_keys(0)
    saved = [s.target for s in store.load_chapter(0).text_segments]
    assert saved and all(t is not None for t in saved)
    run()
    assert store.pending_chapters() == []
    assert saved == [s.target for s in store.load_chapter(0).text_segments]
    assert len(body) == 2 and all("LOCAL GUIDANCE" not in prompt for prompt in body)
    assert "LOCAL GUIDANCE" in (store.get_term("Alice").note or "")
    resumed_notes = [call for call in client.calls if call["operation"] == "glossary.note"]
    assert "LOCAL GUIDANCE" in resumed_notes[-1]["messages"][-1]["content"]
    assert not any(c["operation"].startswith("terminology.") for c in client.calls)


def test_edited_note_wins_while_proposal_is_pending(tmp_path, monkeypatch):
    store, _ = book_store(tmp_path)
    store.upsert_term(GlossaryTerm("May", "梅"))
    agent = GlossaryNoteAgent(FakeClient(), config())
    service = GlossaryNotes(agent, store)
    original_publish = service._publish

    def edited(key, source, record):
        store.upsert_term(GlossaryTerm("May", "梅", note="editor chose this"))
        original_publish(key, source, record)

    monkeypatch.setattr(service, "_publish", edited)
    monkeypatch.setattr(
        agent, "update", lambda *args: NoteResult(lines=["[guidance c0:s0:0-3] draft"])
    )
    service.maintain(0, 0, [Segment(index=0, source="May", target="梅")])
    assert require_value(store.get_term("May")).note == "editor chose this"


def test_precision_cache_binds_to_visible_note_guidance_only(tmp_path):
    store, chapters = book_store(tmp_path)

    def snapshot(guideline, rationale, unrelated, workers=2):
        terms = [
            GlossaryTerm(
                "Carl",
                "卡尔",
                note=f"[guidance c0:s0:0-1] {guideline}\n[rationale c0:s0:0-1] {rationale}",
            ),
            GlossaryTerm("Zed", "泽德", note=unrelated),
        ]
        plan = BatchPlan.capture(0, 0, chapters[0].text_segments, terms, "", "", "", "", [[]], "")
        return _PrecisionRun(
            FakeClient(), config(workers), plan, store, checkpoint=None, progress=None
        ).fingerprint

    initial = snapshot("use A", "reason", "other")
    assert snapshot("use A", "later reason", "edited") == initial
    assert snapshot("use B", "reason", "other") != initial
    assert snapshot("use A", "reason", "other", 0) != initial


def test_note_mode_requires_an_opted_in_runner_and_has_its_own_translation_route(tmp_path):
    assert "glossary.note" not in configured_operations(config(0), "translate")
    assert "glossary.note" in configured_operations(config(), "translate")
    assert "glossary.note" not in configured_operations(config(), "prepare")
    with pytest.raises(ValueError, match="opted-in workflow runner"):
        Orchestrator(config(), client=FakeClient()).prepare(str(tmp_path / "absent"))
    with pytest.raises(ValueError, match="has been removed"):
        Config.from_dict({"pipeline": {"terminology_context": True, "glossary_note_workers": 1}})
    for workers in [-1, 17]:
        with pytest.raises(ValueError):
            config(workers)
