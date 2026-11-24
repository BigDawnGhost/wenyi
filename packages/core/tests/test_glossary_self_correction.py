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

    def test_one_word_capitalized_names_are_kept(self):
        payload = [
            {"source": "格蕾丝", "target": "Grace", "type": "person"},
            {"source": "金", "target": "King", "type": "person"},
            {"source": "俺", "target": "I", "type": "person"},
            {"source": "圣上", "target": "Majesty", "type": "appellation"},
        ]
        result = _extract(payload, "格蕾丝与金面见圣上，俺曰可。", "Text.")
        assert [t.source for t in result] == ["格蕾丝", "金"]

    def test_regional_english_inherits_the_word_list(self):
        payload = [{"source": "大人", "target": "Your Excellency", "type": "honorific"}]
        assert _extract(payload, "李大人到。", "Text.", "ja", "en-GB") == []


def _observe(tracker, pairs, chapter=0):
    """Add pairs at the next positions of one chapter, in arrival order."""
    start = sum(1 for position, _, _ in tracker._pairs if position[0] == chapter)
    tracker.observe(
        ((chapter, start + offset), source, target) for offset, (source, target) in enumerate(pairs)
    )


def _term(source, target, *, first_chapter=0, **kwargs):
    return GlossaryTerm(source=source, target=target, first_chapter=first_chapter, **kwargs)


class TestGlossaryAdherence:
    def test_overridden_extracted_mapping_is_left_out_after_enough_evidence(self):
        tracker = GlossaryAdherence()
        lone = _term("孤", "lone", type="appellation")
        name = _term("林远", "Lin Yuan", type="person")
        # Extraction learned "lone" from 孤舟; later paragraphs use 孤 as "I".
        _observe(tracker, [("孤舟一叶，林远独坐。", "A lone boat; Lin Yuan sat alone.")])
        _observe(tracker, [("孤意已决。", "My mind is made up.")] * 3)
        # Four occurrences are not enough evidence yet.
        assert tracker.filter([lone, name]) == ([lone, name], [])
        _observe(tracker, [("林远与孤同行。", "Lin Yuan walked with me.")])
        _observe(tracker, [("孤问林远。", "I asked Lin Yuan.")])
        # 孤: used in 1 of 6 paragraphs. 林远: used in all 3.
        assert tracker.filter([lone, name]) == ([name], ["孤"])

    def test_user_terms_and_context_dependent_terms_are_always_sent(self):
        tracker = GlossaryAdherence()
        _observe(tracker, [("大人请。", "Your Excellency, please.")])
        _observe(tracker, [("大人请。", "Please, my lord.")] * 6)
        imported = GlossaryTerm(source="大人", target="Your Excellency")
        senses = _term("大人", "Your Excellency; adults", status=STATUS_CONTEXT_DEPENDENT)
        assert tracker.ignored(_term("大人", "Your Excellency"))
        assert tracker.filter([imported]) == ([imported], [])
        assert tracker.filter([senses]) == ([senses], [])

    def test_target_matches_whole_words_case_insensitively_without_article(self):
        tracker = GlossaryAdherence()
        _observe(tracker, [("禄山来了。", "Lu came.")])
        _observe(tracker, [("禄山来了。", "An Lushan came.")] * 6)
        assert tracker.ignored(_term("禄山", "Lu"))
        assert not tracker.ignored(_term("禄山", "the  an lushan"))

    def test_unspaced_scripts_match_targets_inside_running_text(self):
        tracker = GlossaryAdherence()
        _observe(
            tracker,
            [
                ("Alice met the Doctor.", "爱丽丝见到了医生。"),
                ("Then Alice smiled.", "然后爱丽丝笑了。"),
            ]
            * 3,
        )
        _observe(tracker, [("Harry looked up.", "ハリーは顔を上げた。")] * 5)
        assert not tracker.ignored(_term("Alice", "爱丽丝", type="person"))
        assert not tracker.ignored(_term("Harry", "ハリー", type="person"))

    def test_counting_starts_at_the_first_paragraph_using_the_current_target(self):
        tracker = GlossaryAdherence()
        _observe(tracker, [("林风走进来。", "Lin Feng walked in.")] * 20)
        # A user correction is not judged on paragraphs written before it.
        assert not tracker.ignored(_term("林风", "Lin Fung", type="person"))
        _observe(tracker, [("林风笑了。", "Lin Fung smiled.")] + [("林风走了。", "He left.")] * 3)
        assert not tracker.ignored(_term("林风", "Lin Fung", type="person"))
        _observe(tracker, [("林风走了。", "He left.")] * 2)
        assert tracker.ignored(_term("林风", "Lin Fung", type="person"))

    def test_a_new_target_is_judged_from_scratch_on_earlier_paragraphs(self):
        tracker = GlossaryAdherence()
        _observe(tracker, [("白露横江。", "White dew lay across the river.")])
        _observe(tracker, [("横江在望。", "Hengjiang was in sight.")] * 5)
        assert tracker.ignored(_term("横江", "lay across the river"))
        assert not tracker.ignored(_term("横江", "Hengjiang"))

    def test_the_decision_depends_on_book_position_not_arrival_order(self):
        lone = _term("孤", "lone", type="appellation", first_chapter=5)
        later = [("孤舟一叶。", "A lone boat.")]
        earlier = [("孤意已决。", "My mind is made up.")] * 4
        # Chapter 5 translated first on its own, then chapter 1 by a later full run.
        live = GlossaryAdherence()
        _observe(live, later, chapter=5)
        _observe(live, earlier, chapter=1)
        resumed = GlossaryAdherence()
        _observe(resumed, earlier, chapter=1)
        _observe(resumed, later, chapter=5)
        assert live.ignored(lone) is resumed.ignored(lone) is False

    def test_a_retranslated_paragraph_replaces_the_earlier_one(self):
        tracker = GlossaryAdherence()
        _observe(tracker, [("大人请。", "Your Excellency, please.")])
        _observe(tracker, [("大人请。", "Please, my lord.")] * 5)
        assert tracker.ignored(_term("大人", "Your Excellency"))
        tracker.observe(((0, i), "大人请。", "Your Excellency, please.") for i in range(1, 6))
        assert not tracker.ignored(_term("大人", "Your Excellency"))

    def test_aliases_do_not_count_against_the_main_name(self):
        tracker = GlossaryAdherence()
        _observe(tracker, [("孙悟空来了。", "Sun Wukong came.")])
        _observe(tracker, [("大圣笑了。", "The Great Sage laughed.")] * 6)
        assert not tracker.ignored(_term("孙悟空", "Sun Wukong", type="person", aliases=["大圣"]))


BOOK = "\n\n".join(
    ["Alice met the Doctor, a physician."]
    + ["Alice met the Doctor."] * 5
    + ["Alice thanked the Doctor again."]
    + ["The end."]
)
_NUMBERED = re.compile(r"^\[(\d+)\] (.*)$", re.MULTILINE)


def _handler(messages, tier, json_mode):
    system = messages[0]["content"]
    user = messages[-1]["content"]
    if "terminology" in system and "extractor" in system:
        return json.dumps({"terms": []})
    if "literary translator" in system and "previous JSON response" not in user:
        block = user.split("paragraphs to translate]", 1)[1].split("\n\n", 1)[0]
        # The translator always writes 爱丽丝 for Alice and 大夫 for the Doctor ...
        sources = [text for _, text in _NUMBERED.findall(block)]
        # ... except in the first paragraph, where extraction would have learned 医生.
        targets = [
            text.replace("Alice", "爱丽丝").replace(
                "Doctor", "医生" if "physician" in text else "大夫"
            )
            for text in sources
        ]
        return json.dumps({"translations": targets}, ensure_ascii=False)
    return routing_handler(messages, tier, json_mode)


def _setup(tmp_path, **pipeline):
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
                **pipeline,
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


@pytest.fixture
def run(tmp_path):
    return _setup(tmp_path)


def _prompt_entries(client) -> list[list[str]]:
    blocks = []
    for call in client.calls:
        if call["operation"] != "translation.body":
            continue
        system = call["messages"][0]["content"]
        block = system.split("<glossary_data>\n", 1)[1].split("\n</glossary_data>", 1)[0]
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


@pytest.mark.parametrize("polish_concurrency", [1, 2])
def test_polished_text_does_not_change_the_decision(tmp_path, polish_concurrency):
    # The fake polisher rewrites every paragraph without 爱丽丝; drafts are what count.
    orchestrator, store, source, client = _setup(
        tmp_path, polish=True, polish_concurrency=polish_concurrency
    )
    orchestrator.run(str(source))
    assert _prompt_entries(client) == [["Alice", "Doctor"]] * 5 + [["Alice"]] * 3


def test_resumed_run_recomputes_the_decision_from_saved_translations(run, tmp_path):
    orchestrator, store, source, client = run
    chapter = store.load_chapter(0)
    for segment in chapter.text_segments[:6]:
        doctor = "医生" if "physician" in segment.source else "大夫"
        segment.target = segment.source.replace("Alice", "爱丽丝").replace("Doctor", doctor)
    store.save_chapter(chapter)
    orchestrator.run(str(source))
    assert _prompt_entries(client) == [["Alice"]] * 2


def test_imported_mapping_is_sent_even_when_overridden(run):
    orchestrator, store, source, client = run
    store.delete_term("Doctor")
    store.upsert_term(GlossaryTerm(source="Doctor", target="医生", type="term"))
    orchestrator.run(str(source))
    assert _prompt_entries(client) == [["Alice", "Doctor"]] * 8


def test_a_resolved_mapping_is_the_users_and_always_sent(run):
    orchestrator, store, source, client = run
    assert store.resolve_term("Doctor", "医生")
    orchestrator.run(str(source))
    assert _prompt_entries(client) == [["Alice", "Doctor"]] * 8
