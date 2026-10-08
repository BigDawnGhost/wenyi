"""One-shot precision generation and synthesis share standard output-limit behavior."""

import json
from dataclasses import replace

import pytest
from wenyi_core.agents.precision import PrecisionAgent, PrecisionError, PrecisionInputs
from wenyi_core.config import Config
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.llm.retrying import TruncatedResponseError


def inputs(*sources, allow_empty=False):
    return PrecisionInputs(
        tuple(sources),
        (),
        "prior",
        "style",
        "synopsis",
        "digest",
        [[] for _ in sources],
        "lookahead",
        allow_empty,
    )


def agent(response=None, handler=None):
    config = Config.from_dict(
        {
            "language": {"source": "en", "target": "zh"},
            "llm": {"preset": "fake"},
            "pipeline": {"translation_mode": "best_of_three", "align_retry_limit": 5},
        }
    )
    client = FakeClient(handler=handler or (lambda *_: json.dumps(response)))
    return PrecisionAgent(client, config), client


def test_synthesis_sees_all_distinct_drafts_source_and_context_without_output_hint():
    worker, client = agent({"translations": ["综合稿"]})
    packet = inputs("original")
    for _ in range(3):
        worker.translate(packet)
    assert worker.synthesize(packet, [["甲稿"], ["乙稿"], ["丙稿"]]) == ["综合稿"]
    assert len(client.calls) == 4
    assert all(call["max_tokens"] is None for call in client.calls)
    assert all(call["messages"][:2] == client.calls[0]["messages"][:2] for call in client.calls)
    assert [call["operation"] for call in client.calls] == ["translation.body"] * 3 + [
        "polish.body"
    ]
    task = json.loads(client.calls[-1]["messages"][-1]["content"].split("Task (JSON):\n")[1])
    assert task["drafts"] == [["甲稿"], ["乙稿"], ["丙稿"]]
    assert "original" in client.calls[-1]["messages"][1]["content"]
    assert all(
        message["role"] != "assistant" for call in client.calls for message in call["messages"]
    )
    assert "甲稿" not in str(client.calls[0]["messages"])


def test_duplicate_draws_are_not_multiple_votes():
    worker, client = agent({"translations": ["润色稿"]})
    worker.synthesize(inputs("source"), [["初稿"], ["初稿"], ["初稿"]])
    task = json.loads(client.calls[0]["messages"][-1]["content"].split("Task (JSON):\n")[1])
    assert task["drafts"] == [["初稿"]]


@pytest.mark.parametrize(
    "response", [{}, [], {"translations": []}, {"translations": [3]}, {"translations": [""]}]
)
def test_invalid_output_fails_once_without_alignment_or_extra_model_calls(response):
    worker, client = agent(response)
    with pytest.raises(PrecisionError):
        worker.translate(inputs("source"))
    assert len(client.calls) == 1


def test_truncation_does_not_add_a_small_budget_or_split_context():
    def truncated(*_):
        raise TruncatedResponseError("provider output ended")

    worker, client = agent(handler=truncated)
    with pytest.raises(PrecisionError, match="truncated"):
        worker.translate(inputs("one", "two"))
    assert len(client.calls) == 1
    assert client.calls[0]["max_tokens"] is None
    assert '"sources": ["one", "two"]' in client.calls[0]["messages"][1]["content"]


def test_protected_positions_and_intentional_blank_targets():
    worker, client = agent({"translations": ["", "123"]})
    packet = inputs("OCR", "123", allow_empty=True)
    assert worker.translate(packet) == ["", "123"]
    assert worker.synthesize(packet, [["", "123"]] * 3) == ["", "123"]
    assert worker.translate(inputs("123", "---")) == ["123", "---"]
    assert len(client.calls) == 2


@pytest.mark.parametrize("allow_empty", [False, True])
@pytest.mark.parametrize(
    ("source", "generated"),
    [
        ("10\u2005\u20059\u2005\u20058", "10 9 8"),
        ("10\u2005\u20059\u2005\u20058", ""),
        ("123", "一二三"),
        ("---", "separator"),
        ("\t\u2005\n", "added text"),
        ("", "added text"),
    ],
)
def test_protected_outputs_use_source_in_drafts_and_synthesis(source, generated, allow_empty):
    worker, client = agent({"translations": ["译文", generated]})
    packet = inputs("source", source, allow_empty=allow_empty)
    assert worker.translate(packet) == ["译文", source]
    drafts = [["甲稿", generated], ["乙稿", "changed"], ["丙稿", ""]]
    assert worker.synthesize(packet, drafts) == ["译文", source]
    task = json.loads(client.calls[-1]["messages"][-1]["content"].split("Task (JSON):\n")[1])
    assert task["drafts"] == [["甲稿", source], ["乙稿", source], ["丙稿", source]]
    assert drafts == [["甲稿", generated], ["乙稿", "changed"], ["丙稿", ""]]
    assert len(client.calls) == 2


@pytest.mark.parametrize("targets", [["译文"], ["译文", "123", "extra"], ["译文", None]])
def test_protected_outputs_still_require_matching_count_and_string_types(targets):
    worker, client = agent({"translations": targets})
    with pytest.raises(PrecisionError):
        worker.translate(inputs("source", "123"))
    assert len(client.calls) == 1


def test_book_prefix_is_stable_before_batch_context():
    worker, client = agent({"translations": ["二"]})
    packet = inputs("two")
    worker.translate(packet)
    worker.translate(replace(packet, context="new context", chapter_digest="next chapter"))
    first, second = [call["messages"][1]["content"] for call in client.calls]
    assert first.split('"chapter_digest"')[0] == second.split('"chapter_digest"')[0]


def test_synthesis_requires_three_structurally_usable_drafts():
    worker, client = agent({"translations": ["稿"]})
    with pytest.raises(PrecisionError):
        worker.synthesize(inputs("source"), [["稿"]])
    assert not client.calls
