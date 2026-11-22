"""Glossary entries that cannot be right everywhere stay out of prompts.

Two language-agnostic signals replace per-book word lists: a target made only of pronouns
and title words ("I", "your son", "Your Excellency") names nobody, and an extracted mapping
the translator keeps overriding is shown wrong by the finished translation itself.
"""

from __future__ import annotations

import json
import re

import pytest
from wenyi_core.agents.glossary_extractor import GlossaryExtractor
from wenyi_core.config import Config
from wenyi_core.glossary.adherence import GlossaryAdherence
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.models.glossary import STATUS_CONTEXT_DEPENDENT, GlossaryTerm
from wenyi_core.pipeline.orchestrator import Orchestrator

from tests.fake_llm import routing_handler


def _extract(payload: list[dict], source: str, target: str, src: str = "zh", tgt: str = "en"):
    config = Config.from_dict(
        {"language": {"source": src, "target": tgt}, "llm": {"preset": "fake"}}
    )
    items = [{"scope": "stable", **item} for item in payload]

    def handler(messages, tier, json_mode):
        return json.dumps({"terms": items}, ensure_ascii=False)

    return GlossaryExtractor(FakeClient(handler=handler), config).extract(source, target, [])


class TestGenericTargets:
    def test_pronoun_and_bare_title_targets_are_not_extracted(self):
        # Self-references and titles absent from the Chinese source word list.
        payload = [
            {"source": "贫僧", "target": "this poor monk", "type": "appellation"},
            {"source": "俺老孙", "target": "I", "type": "appellation"},
            {"source": "师父", "target": "Master", "type": "appellation"},
            {"source": "本小姐", "target": "this young lady", "type": "appellation"},
            {"source": "儿臣", "target": "your son", "type": "appellation"},
            {"source": "母后", "target": "my mother", "type": "appellation"},
            {"source": "孙悟空", "target": "Sun Wukong", "type": "person"},
            {"source": "齐天大圣", "target": "Great Sage Equal to Heaven", "type": "appellation"},
            {"source": "唐僧", "target": "Tang Monk", "type": "person"},
            {"source": "江大人", "target": "Lord Jiang", "type": "appellation"},
        ]
        source = "贫僧与俺老孙、师父、本小姐、儿臣、母后，孙悟空即齐天大圣，唐僧见江大人。"
        result = _extract(payload, source, "Text.")
        assert [t.source for t in result] == ["孙悟空", "齐天大圣", "唐僧", "江大人"]

    def test_targets_in_other_languages_are_not_judged_by_english_words(self):
        payload = [{"source": "Master", "target": "师父", "type": "appellation"}]
        assert [t.source for t in _extract(payload, "Master spoke.", "师父说。", "en", "zh")] == [
            "Master"
        ]
        assert [t.source for t in _extract(payload, "Master.", "Maître.", "en", "fr")] == ["Master"]

    def test_regional_english_inherits_the_word_list(self):
        payload = [{"source": "大人", "target": "Your Excellency", "type": "honorific"}]
        assert _extract(payload, "李大人到。", "Text.", "ja", "en-GB") == []


def _term(source, target, *, first_chapter=0, **kwargs):
    return GlossaryTerm(source=source, target=target, first_chapter=first_chapter, **kwargs)


class TestGlossaryAdherence:
    def test_overridden_extracted_mapping_is_left_out_after_enough_evidence(self):
        tracker = GlossaryAdherence()
        lone = _term("孤", "lone", type="appellation")
        name = _term("江哲", "Jiang Zhe", type="person")
        tracker.observe([("孤意已决。", "My mind is made up.")] * 3)
        tracker.observe([("江哲与孤同行。", "Jiang Zhe walked with me.")])
        # Four occurrences are not enough evidence yet.
        assert tracker.filter([lone, name]) == ([lone, name], [])
        tracker.observe([("孤舟一叶，江哲独坐。", "A lone boat; Jiang Zhe sat alone.")])
        tracker.observe([("孤问江哲。", "I asked Jiang Zhe.")])
        # 孤: used in 1 of 6 paragraphs. 江哲: used in all 3.
        assert tracker.filter([lone, name]) == ([name], ["孤"])

    def test_user_terms_and_context_dependent_terms_are_always_sent(self):
        tracker = GlossaryAdherence()
        tracker.observe([("大人请。", "Please, my lord.")] * 6)
        imported = GlossaryTerm(source="大人", target="Your Excellency")
        senses = _term("大人", "Your Excellency; adults", status=STATUS_CONTEXT_DEPENDENT)
        assert tracker.filter([imported]) == ([imported], [])
        assert tracker.filter([senses]) == ([senses], [])

    def test_target_matches_whole_words_case_insensitively_without_article(self):
        tracker = GlossaryAdherence()
        tracker.observe([("禄山来了。", "An Lushan came.")] * 6)
        assert tracker.ignored(_term("禄山", "Lu"))
        assert not tracker.ignored(_term("禄山", "the  an lushan"))

    def test_a_new_target_is_judged_from_scratch_on_earlier_paragraphs(self):
        tracker = GlossaryAdherence()
        tracker.observe([("横江在望。", "Hengjiang was in sight.")] * 5)
        assert tracker.ignored(_term("横江", "lay across the river"))
        assert not tracker.ignored(_term("横江", "Hengjiang"))

    def test_aliases_count_as_occurrences_for_named_entities(self):
        tracker = GlossaryAdherence()
        tracker.observe([("随云笑了。", "Suiyun smiled.")] * 5)
        assert tracker.ignored(_term("江哲", "Jiang Zhe", type="person", aliases=["随云"]))


BOOK = "\n\n".join(
    ["Alice met the Doctor."] * 6 + ["Alice thanked the Doctor again."] + ["The end."]
)
_NUMBERED = re.compile(r"^\[(\d+)\] (.*)$", re.MULTILINE)


def _handler(messages, tier, json_mode):
    system = messages[0]["content"]
    user = messages[-1]["content"]
    if "terminology" in system and "extractor" in system:
        return json.dumps({"terms": []})
    if "literary translator" in system and "previous JSON response" not in user:
        block = user.split("paragraphs to translate]", 1)[1].split("\n\n", 1)[0]
        # The translator always writes 爱丽丝 for Alice and never 医生 for the Doctor.
        sources = [text for _, text in _NUMBERED.findall(block)]
        targets = [text.replace("Alice", "爱丽丝").replace("Doctor", "大夫") for text in sources]
        return json.dumps({"translations": targets}, ensure_ascii=False)
    return routing_handler(messages, tier, json_mode)


@pytest.fixture
def run(tmp_path):
    config = Config.from_dict(
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
    source = tmp_path / "book.txt"
    source.write_text(BOOK, encoding="utf-8")
    client = FakeClient(handler=_handler)
    orchestrator = Orchestrator(config, client=client)
    store = orchestrator.prepare(str(source))
    store.upsert_term(GlossaryTerm(source="Alice", target="爱丽丝", type="person"), chapter=0)
    store.upsert_term(GlossaryTerm(source="Doctor", target="医生", type="term"), chapter=0)
    return orchestrator, store, source, client


def _prompt_entries(client) -> list[list[str]]:
    blocks = []
    for call in client.calls:
        if call["operation"] != "translation.body":
            continue
        user = call["messages"][-1]["content"]
        block = user.split("[Glossary] (required mappings)\n", 1)[1].split("\n\n", 1)[0]
        blocks.append(re.findall(r"^- (\S+) ", block, re.MULTILINE))
    return blocks


def _events(store, name):
    with open(store.event_log_path, encoding="utf-8") as stream:
        events = [json.loads(line) for line in stream if line.strip()]
    return [event for event in events if event["event"] == name]


def test_translation_stops_sending_an_extracted_mapping_it_keeps_overriding(run):
    orchestrator, store, source, client = run
    orchestrator.run(str(source))
    entries = _prompt_entries(client)
    assert len(entries) == 8
    # Five paragraphs without 医生 are enough evidence; later prompts leave it out.
    assert entries[:5] == [["Alice", "Doctor"]] * 5
    assert entries[5:] == [["Alice"]] * 3
    assert store.get_term("Doctor") is not None
    assert [(e["added"], e["restored"]) for e in _events(store, "glossary_terms_left_out")] == [
        (["Doctor"], [])
    ]


def test_resumed_run_recomputes_the_decision_from_saved_translations(run, tmp_path):
    orchestrator, store, source, client = run
    chapter = store.load_chapter(0)
    for segment in chapter.text_segments[:6]:
        segment.target = segment.source.replace("Alice", "爱丽丝").replace("Doctor", "大夫")
    store.save_chapter(chapter)
    orchestrator.run(str(source))
    assert _prompt_entries(client) == [["Alice"]] * 2


def test_imported_mapping_is_sent_even_when_overridden(run):
    orchestrator, store, source, client = run
    store.delete_term("Doctor")
    store.upsert_term(GlossaryTerm(source="Doctor", target="医生", type="term"))
    orchestrator.run(str(source))
    assert _prompt_entries(client) == [["Alice", "Doctor"]] * 8
