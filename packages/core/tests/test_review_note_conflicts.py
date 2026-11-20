"""Glossary discrepancies are observations, not paragraph patches."""

import json

import pytest
from wenyi_core.agents.reviewer import Reviewer, ReviewOutputError
from wenyi_core.config import Config
from wenyi_core.glossary.notes import Fact, NoteJournal
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.models.glossary import GlossaryTerm
from wenyi_core.pipeline.orchestrator import Orchestrator
from wenyi_core.review.evidence import BookEvidenceIndex
from wenyi_core.review.run_store import ReviewRunStore

from tests.review_fixtures import autofix_config, autofix_store


def scan(index=1):
    fact = Fact(text="In court, address May as Judge May.", when=["court"], refs=["c0:s0:0-10"])
    terms = [GlossaryTerm("May", "梅", note=fact.line())]
    answer = {
        "issues": [],
        "reviewed_segments": 2,
        "complete": True,
        "glossary_alerts": [{"index": index, "note_id": 0, "detail": "法庭称谓需核对。"}],
    }
    client = FakeClient(lambda *_: json.dumps(answer))
    result = Reviewer(client, Config(source_lang="en", target_lang="zh")).review_result(
        ["May arrived.", "May spoke in court."], ["梅到了。", "梅法官发言了。"], terms
    )
    return result, client


def test_saved_scoped_note_conflict_is_reported_even_without_a_text_issue_or_workers():
    result, client = scan()
    assert result.issues == []
    assert result.glossary_alerts == [
        {
            "index": 1,
            "term": "May",
            "stored_target": "梅",
            "note": "In court, address May as Judge May.",
            "detail": "法庭称谓需核对。",
        }
    ]
    assert len(client.calls) == 1
    assert '"applies_to": [1]' in client.calls[0]["messages"][1]["content"]


def test_model_cannot_attribute_a_scoped_note_to_an_unrelated_paragraph():
    with pytest.raises(ReviewOutputError, match="invalid_glossary_alert"):
        scan(index=0)


def test_glossary_only_alert_survives_review_with_no_fixer_or_glossary_edits(tmp_path):
    store = autofix_store(str(tmp_path))
    term = GlossaryTerm("原文", "旧译", note="此处用另一译法。")
    store.upsert_term(term)
    answer = {
        "issues": [],
        "reviewed_segments": 1,
        "complete": True,
        "glossary_alerts": [{"index": 0, "note_id": 0, "detail": "请人工核对词条。"}],
    }
    client = FakeClient(lambda *_: json.dumps(answer))
    orch = Orchestrator(autofix_config(str(tmp_path)), client=client, storage=store)
    before = store.load_chapter(0)
    with store.lock():
        result = orch._run_review_locked(store, [term], progress=None)
    assert result.result["summary"]["glossary_alert_count"] == 1
    assert result.issues == [] and result.changes == []
    assert {c["operation"] for c in client.calls} == {"review.scan"}
    stored = store.get_term("原文")
    assert store.load_chapter(0) == before and stored is not None and stored.note == term.note
    alerts = store.read_artifact(
        f"reviews/{result.result['review_id']}/rounds/final/glossary_alerts.json"
    )
    assert isinstance(alerts, list)
    assert [(a["chapter"], a["index"], a["stored_target"]) for a in alerts] == [(0, 0, "旧译")]
    reopened = ReviewRunStore.open_existing(result.run_dir, storage=store)
    assert reopened.glossary_alerts() == alerts


def test_review_tool_pages_history_but_never_previews_superseded_guidance():
    earlier = Fact(text="Old wording.", refs=["c0:s0:0-5"])
    old_id = next(iter(NoteJournal(earlier.line()).active))
    new = Fact(
        text="New wording.",
        when=["court"],
        refs=["c1:s0:0-6"],
        corrects=old_id,
        reason="The title is now explicit.",
    )
    term = GlossaryTerm("May", "梅", aliases=["M"], note=f"{earlier.line()}\n{new.line()}")
    idx = BookEvidenceIndex([], [term], {})

    def query(**kwargs):
        return idx.execute(
            {"request_id": "lookup", "tool": "glossary_term", "arguments": {"term": "M", **kwargs}}
        )

    preview = query()["term"]["note"]
    assert "New wording." in preview and "court" in preview and "Old wording." not in preview
    page = query(section="history", offset=0)["note_page"]
    assert "Old wording." in page["text"]
    assert query(section="history", offset=5)["note_page"]["text"] == page["text"][5:]
    assert not query(section="anything")["ok"]
