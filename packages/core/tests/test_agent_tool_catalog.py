"""Prefix sharing and capability boundaries for model-requested reads."""

import json
from dataclasses import replace
from os.path import commonprefix

import pytest
from wenyi_core.agent_tools import (
    BODY_TOOLS,
    NOTE_TOOLS,
    REVIEW_TOOLS,
    BoundTools,
    ToolError,
    describe,
)
from wenyi_core.agents.glossary_note import GlossaryNoteAgent, NoteWindow
from wenyi_core.agents.note_tools import batch_note_tools
from wenyi_core.config import Config
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.models.glossary import GlossaryTerm


def test_two_workers_share_evidence_prefix_without_padding():
    client = FakeClient(lambda *_: '{"updates":[]}')
    agent = GlossaryNoteAgent(client, Config(source_lang="en", target_lang="zh"))
    window = NoteWindow(0, 0, (("May and Lee waited.", "两个人都在等。"),))
    for name in ("May", "Lee"):
        agent.update(GlossaryTerm(name, name), window, [0])
    a, b = (call["messages"] for call in client.calls)
    assert a[0] == b[0]
    shared = commonprefix([a[1]["content"], b[1]["content"]])
    assert window.pairs[0][0] in shared and window.pairs[0][1] in shared
    assert json.loads(a[1]["content"])["term"] == "May"


def test_body_tools_reject_foreign_names_keys_and_terms():
    tools = batch_note_tools(
        [GlossaryTerm("May", "梅", note="local"), GlossaryTerm("Lee", "李", note="private")], "May"
    )
    assert tools is not None
    for name, args in [
        ("read_context", {"index": 0}),
        ("book_context", {"section": "style_guide"}),
        ("read_note", {"term": "May"}),
        ("read_note", {"term": "May", "section": "editor", "other": 1}),
        ("read_note", {"term": "Lee", "section": "editor"}),
        ("read_note", []),
    ]:
        with pytest.raises(ToolError):
            tools.call(name, args)
    assert tools.call("read_note", {"term": "May", "section": "editor"})["text"] == "local"
    with pytest.raises(ValueError, match="do not match"):
        BoundTools(BODY_TOOLS)


def test_note_cache_fingerprints_only_the_maintenance_prompt_and_its_tools():
    cfg = Config.from_dict({"pipeline": {"glossary_note_workers": 4}})
    plan = cfg.language_policy("translation")
    vals = dict(plan.prompt_values)
    for key, specs in [
        ("body_tools", BODY_TOOLS),
        ("note_tools", NOTE_TOOLS),
        ("review_evidence_tools", REVIEW_TOOLS),
    ]:
        assert describe(specs) in vals[key]

    def key_with(change):
        changed = {**vals, change: "changed"}
        return replace(plan, prompt_values=tuple(sorted(changed.items()))).task_fingerprint(
            "glossary_note_system"
        )

    key = plan.task_fingerprint("glossary_note_system")
    assert key_with("note_tools") != key
    assert key_with("body_tools") == key
