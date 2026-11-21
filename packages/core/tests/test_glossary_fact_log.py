"""Independent facts, evidence-backed corrections and contextual forms of address."""

import json

import pytest
from wenyi_core.agents.glossary_extractor import GlossaryExtractor, TranslatedSegmentEvidence
from wenyi_core.agents.glossary_note import GlossaryNoteAgent, NoteWindow
from wenyi_core.config import Config
from wenyi_core.glossary.notes import Fact, NoteJournal, guidance, note_page
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.models.glossary import GlossaryTerm


def config():
    return Config.from_dict(
        {
            "language": {"source": "en", "target": "zh"},
            "llm": {"preset": "fake"},
            "pipeline": {"glossary_note_workers": 2},
        }
    )


def test_second_fact_does_not_replace_unrelated_first_fact():
    term = GlossaryTerm("Ada", "艾达")
    for i, text in enumerate(("Ada is Bo's sister.", "Ada teaches music.")):
        window = NoteWindow(0, i, ((text, "译文"),))
        ref = window.read(i, 0)["ref"]
        reply = {"updates": [{"text": text, "refs": [ref]}]}
        agent = GlossaryNoteAgent(FakeClient(lambda *_: json.dumps(reply)), config())
        term.note = "\n".join(filter(None, [term.note, *agent.update(term, window, [i]).lines]))
    assert "Bo's sister" in guidance(term.note) and "teaches music" in guidance(term.note)


@pytest.mark.parametrize("replacement", ["Lee", None, "absent", ""])
def test_one_term_scoped_repair_never_publishes_an_unconditional_fact(replacement):
    window = NoteWindow(0, 0, (("Ada met Lee.", "译"),))
    ref = window.read(0, 0)["ref"]
    updates = [
        {"text": "Unrelated fact.", "refs": [ref]},
        {"text": "A scoped fact.", "refs": [ref], "when": ["Ada", "李"]},
    ]

    def handler(messages, *_):
        if len(messages) == 2:
            return json.dumps({"updates": updates})
        assert json.loads(messages[-2]["content"])["updates"] == updates
        assert "updates[1].when[1]" in messages[-1]["content"]
        return json.dumps({"replacements": [replacement]})

    client = FakeClient(handler)
    agent = GlossaryNoteAgent(client, config())
    result = agent.update(GlossaryTerm("Ada", "艾达"), window, [0])
    assert result.outcome == ("accepted" if replacement == "Lee" else "partial")
    log = NoteJournal("\n".join(result.lines))
    assert [fact.when for fact in log.active.values()] == (
        [[], ["Ada", "Lee"]] if replacement == "Lee" else [[]]
    )
    assert guidance("\n".join(result.lines), "unrelated source") == "Unrelated fact."
    assert len(client.calls) == 2


def test_correction_reopens_old_evidence_and_retires_only_one_fact():
    old = "Ada called Bo her brother."
    old_ref = f"c0:s0:0-{len(old)}"
    unrelated = "Teaches music."
    note = "\n".join(
        [
            "Editor",
            Fact(text="Bo may be younger.", refs=[old_ref]).line(),
            Fact(text=unrelated, refs=[old_ref]).line(),
        ]
    )
    key = next(iter(NoteJournal(note).active))
    source = "Bo was already at university before Ada started school."
    new_ref = f"c1:s0:0-{len(source)}"
    evidence = TranslatedSegmentEvidence(0, 0, old, "译")
    window = NoteWindow(1, 0, ((source, "译"),), {(0, 0): evidence})

    def handler(messages, *_):
        if len(messages) == 2:
            return json.dumps({"reads": [{"tool": "read_reference", "ref": old_ref}]})
        assert json.loads(messages[-1]["content"])["results"] == [{"ref": old_ref, "source": old}]
        return json.dumps(
            {
                "updates": [
                    {
                        "text": "Bo is Ada's older brother.",
                        "refs": [old_ref, new_ref],
                        "when": ["Bo"],
                        "corrects": key,
                        "reason": "Their school timeline establishes seniority.",
                    }
                ]
            }
        )

    agent = GlossaryNoteAgent(FakeClient(handler), config())
    term = GlossaryTerm("Ada", "艾达", note=note)
    term.note += "\n" + agent.update(term, window, [0]).lines[0]
    assert guidance(term.note, "Ada met Bo.") == f"Editor\n{unrelated}\nBo is Ada's older brother."
    assert guidance(term.note, "Ada teaches.") == f"Editor\n{unrelated}"
    page = note_page(term.note, "history", 0)["text"]
    assert all(s in page for s in ("Bo may be younger", key, new_ref, old_ref, "timeline"))
    # A replay cannot correct a now-superseded ID.
    assert agent.update(term, window, [0]).outcome == "not_adopted"


def test_correction_requires_new_evidence_and_reason_repetition_is_noop():
    src = "Ada met Bo."
    ref = f"c0:s0:0-{len(src)}"
    fact = Fact(text="They met.", refs=[ref])
    log = NoteJournal(fact.line())
    key = next(iter(log.active))

    def check(updates):
        return GlossaryNoteAgent._validated_lines(updates, {ref: src}, {ref}, log)

    assert check([{"text": "They met.", "refs": [ref]}]) == []
    for change in [
        {"corrects": key},
        {"reason": "changed"},
        {"corrects": key, "reason": "No new evidence."},
        {"corrects": "0" * 16, "reason": "Unknown prior."},
        {"when": ["Absent phrase"]},
        {"refs": ["c3:s4:0-7"]},
    ]:
        with pytest.raises(ValueError):
            check([{"text": "A different conclusion.", "refs": [ref], **change}])


def test_contextual_kinship_does_not_become_a_required_mapping():
    terms = [
        {
            "source": "your brother",
            "target": "你哥哥",
            "type": "appellation",
            "scope": "contextual",
        },
        {"source": "his sister", "target": "他妹妹", "type": "term", "scope": "stable"},
        {"source": "the teacher", "target": "林老师", "type": "appellation"},
        {
            "source": "Aunt May",
            "target": "梅姨",
            "type": "appellation",
            "scope": "stable",
            "aliases": ["your aunt"],
        },
    ]
    extractor = GlossaryExtractor(FakeClient(lambda *_: json.dumps({"terms": terms})), config())
    extracted = extractor.extract("Aunt May met your brother.", "…", [])
    assert [t.source for t in extracted] == ["Aunt May"] and extracted[0].aliases == []
