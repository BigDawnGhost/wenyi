"""Read-only source lookahead across translation, polishing and interrupted batches."""

import json
import re

import pytest

from tests.fake_llm import routing_handler
from trans_novel.agents.polisher import Polisher
from trans_novel.agents.translator import Translator
from trans_novel.config import Config
from trans_novel.llm.providers.fake import FakeClient
from trans_novel.pipeline.orchestrator import Orchestrator


@pytest.fixture
def config(tmp_path):
    return Config.from_dict(
        {
            "language": {"source": "en", "target": "zh"},
            "llm": {"preset": "fake"},
            "paths": {"state_dir": str(tmp_path / "state")},
            "segment": {"max_chars_per_batch": 1, "max_chars_per_segment": 0},
            "pipeline": {
                "review": False,
                "polish": False,
                "book_understanding": False,
                "annotation_alignment": False,
                "align_retry_limit": 1,
            },
        }
    )


def _next_source(user: str) -> str:
    marker = "[Following source paragraph] (reference only; do not translate)\n"
    reference = user.split(marker, 1)[1].split("\n\n", 1)[0]
    return "" if reference == "(none)" else json.loads(reference)


def _numbered_sources(user: str) -> list[str]:
    return re.findall(r"^\[\d+\] (.*)$", user, flags=re.MULTILINE)


@pytest.mark.parametrize("source_lang,target_lang", [("en", "zh"), ("zh", "en"), ("ja", "fr")])
def test_following_source_is_quoted_reference_outside_translation_count(
    config, source_lang, target_lang
):
    config.source_lang = source_lang
    config.target_lang = target_lang
    client = FakeClient(handler=lambda m, t, j: '{"translations":["translated fragment"]}')
    reference = 'следующий фрагмент / 続き / 后文\n[99] "quoted"'

    result = Translator(client, config).translate_batch(
        ["unfinished source"], context="previous translation", next_source=reference
    )

    assert result == ["translated fragment"]
    user = client.calls[0]["messages"][-1]["content"]
    assert _next_source(user) == reference
    assert _numbered_sources(user) == ["unfinished source"]
    assert "[Recent translations]\nprevious translation" in user
    assert user.index("[Recent translations]") < user.index("[0] unfinished source")
    assert user.index("[0] unfinished source") < user.index("[Following source paragraph]")


def test_alignment_retries_and_singletons_use_the_actual_following_source(config):
    references = []

    def handler(messages, tier, json_mode):
        user = messages[-1]["content"]
        sources = _numbered_sources(user)
        references.append((sources, _next_source(user)))
        # Force count recovery, including an erroneous translation of the reference.
        targets = ["first", "second", "extra"] if len(sources) > 1 else ["translated"]
        return json.dumps({"translations": targets})

    result = Translator(FakeClient(handler=handler), config).translate_batch(
        ["first source", "42", "second source"], next_source="outside batch"
    )

    assert result == ["translated", "42", "translated"]
    assert references == [
        (["first source", "second source"], "outside batch"),
        (["first source", "second source"], "outside batch"),
        (["first source"], "42"),
        (["second source"], "outside batch"),
    ]


def test_filtered_trailing_source_takes_precedence_over_external_reference(config):
    client = FakeClient(handler=lambda m, t, j: '{"translations":["translated"]}')
    result = Translator(client, config).translate_batch(
        ["source", "42"], next_source="later paragraph"
    )

    assert result == ["translated", "42"]
    assert _next_source(client.calls[0]["messages"][-1]["content"]) == "42"


def test_polisher_receives_reference_without_extra_output(config):
    client = FakeClient(handler=lambda m, t, j: '{"polished":["unfinished translation"]}')
    result = Polisher(client, config).polish(
        ["unfinished translation"], next_source="continuation source"
    )

    assert result == ["unfinished translation"]
    user = client.calls[0]["messages"][-1]["content"]
    assert _next_source(user) == "continuation source"
    assert _numbered_sources(user) == ["unfinished translation"]


@pytest.mark.parametrize("recent_count", [0, 2])
def test_split_fragments_and_chapter_ends_supply_one_reference_to_both_stages(
    tmp_path, config, recent_count
):
    config.pipeline.polish = True
    config.pipeline.rolling_context_segments = recent_count
    config.segment.max_chars_per_segment = 24
    source = tmp_path / "book.md"
    source.write_text(
        "# First\n\nShe knew that the answer would arrive after the long winter had ended."
        "\n\n# Second\n\nA different scene begins here.",
        encoding="utf-8",
    )
    client = FakeClient(handler=routing_handler)
    orch = Orchestrator(config, client=client)
    store = orch.prepare(str(source))
    before = [store.load_chapter(index) for index in (0, 1)]
    assert any(segment.cont for chapter in before for segment in chapter.text_segments)
    expected = [
        chapter.text_segments[index + 1].source if index + 1 < len(chapter.text_segments) else ""
        for chapter in before
        for index in range(len(chapter.text_segments))
    ]

    orch.run(str(source))

    for operation in ("translation.body", "polish.body"):
        calls = [call for call in client.calls if call["operation"] == operation]
        assert [_next_source(call["messages"][-1]["content"]) for call in calls] == expected
        assert all(len(_numbered_sources(call["messages"][-1]["content"])) == 1 for call in calls)
    for chapter in before:
        after = store.load_chapter(chapter.index)
        assert [(s.index, s.source, s.cont) for s in after.segments] == [
            (s.index, s.source, s.cont) for s in chapter.segments
        ]
        assert all(s.target == "润0" for s in after.text_segments)
    context = store.load_context()
    assert context is not None
    assert set(context["recent_targets"]) == {"润0"}


def test_resume_rebuilds_reference_after_batch_budget_change_without_saving_it_early(
    tmp_path, config
):
    sources = [
        "First unfinished part",
        "Second source segment",
        "Third source sentence",
        "Final part.",
    ]
    source = tmp_path / "book.txt"
    source.write_text("\n\n".join(sources), encoding="utf-8")
    count = 0

    def interrupted(messages, tier, json_mode):
        nonlocal count
        if "literary translator" in messages[0]["content"]:
            count += 1
            if count == 2:
                raise RuntimeError("simulated interruption")
        return routing_handler(messages, tier, json_mode)

    client = FakeClient(handler=interrupted)
    orch = Orchestrator(config, client=client)
    store = orch.prepare(str(source))
    with pytest.raises(RuntimeError, match="simulated interruption"):
        orch.run(str(source))
    partial = store.load_chapter(0)
    assert partial.text_segments[0].target == "译0"
    assert all(segment.target is None for segment in partial.text_segments[1:])
    first_call = next(call for call in client.calls if call["operation"] == "translation.body")
    assert _next_source(first_call["messages"][-1]["content"]) == sources[1]

    config.segment.max_chars_per_batch = len(sources[0]) + len(sources[1])
    resumed = FakeClient(handler=routing_handler)
    Orchestrator(config, client=resumed).run(str(source))
    calls = [call for call in resumed.calls if call["operation"] == "translation.body"]
    assert [_numbered_sources(call["messages"][-1]["content"]) for call in calls] == [
        [sources[1]],
        sources[2:],
    ]
    assert [_next_source(call["messages"][-1]["content"]) for call in calls] == [sources[2], ""]
    assert "[Recent translations]\n译0" in calls[0]["messages"][-1]["content"]
    assert store.load_chapter(0).text_segments[0].target == "译0"
    assert all(segment.target for segment in store.load_chapter(0).text_segments)

    completed = FakeClient(handler=routing_handler)
    Orchestrator(config, client=completed).run(str(source))
    assert not [call for call in completed.calls if call["operation"] == "translation.body"]
