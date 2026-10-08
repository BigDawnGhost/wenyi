"""Precision retries share a structural budget without changing model output limits."""

import json
from dataclasses import replace

import pytest
from wenyi_core.agents.precision import PrecisionAgent, PrecisionError, PrecisionInputs
from wenyi_core.config import Config
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.llm.retrying import ProviderRequestError, TruncatedResponseError


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


def agent(response=None, handler=None, retry_limit=2):
    config = Config.from_dict(
        {
            "language": {"source": "en", "target": "zh"},
            "llm": {"preset": "fake"},
            "pipeline": {"translation_mode": "best_of_three", "align_retry_limit": retry_limit},
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
@pytest.mark.parametrize("stage", ["translate", "synthesize"])
@pytest.mark.parametrize("retry_limit", [0, 2])
def test_invalid_output_exhausts_shared_retry_limit(response, stage, retry_limit):
    worker, client = agent(response, retry_limit=retry_limit)
    packet = inputs("source")
    with pytest.raises(PrecisionError):
        if stage == "translate":
            worker.translate(packet)
        else:
            worker.synthesize(packet, [["稿"]] * 3)
    assert len(client.calls) == retry_limit + 1
    assert all(call["messages"] == client.calls[0]["messages"] for call in client.calls)


@pytest.mark.parametrize("stage", ["translate", "synthesize"])
@pytest.mark.parametrize("failure", ["count", "type", "blank", "json", "truncated"])
def test_invalid_output_retries_full_request_and_recovers(stage, failure):
    attempts = 0

    def respond(*_):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            if failure == "truncated":
                raise TruncatedResponseError("provider output ended")
            if failure == "json":
                return "not JSON"
            targets = {
                "count": ["标题\n\n正文"],
                "type": ["标题", 3],
                "blank": ["标题", ""],
            }[failure]
            return json.dumps({"translations": targets})
        return json.dumps({"translations": ["标题", "正文"]})

    worker, client = agent(handler=respond)
    packet = inputs("Title", "Body")
    if stage == "translate":
        assert worker.translate(packet) == ["标题", "正文"]
    else:
        assert worker.synthesize(packet, [["标题", "正文"]] * 3) == ["标题", "正文"]
    assert len(client.calls) == 2
    operation = "translation.body" if stage == "translate" else "polish.body"
    assert [call["operation"] for call in client.calls] == [operation] * 2
    assert all(call["messages"] == client.calls[0]["messages"] for call in client.calls)
    assert all(call["max_tokens"] is None for call in client.calls)
    assert '"sources": ["Title", "Body"]' in client.calls[-1]["messages"][1]["content"]


@pytest.mark.parametrize("status", [401, 429, 500])
def test_provider_failures_do_not_start_another_retry_loop(status):
    error = ProviderRequestError(
        provider="fake",
        operation="translation.body",
        error_type="http_error",
        status_code=status,
        reason=f"http_{status}" if status != 401 else None,
        resumable=status != 401,
    )

    def fail(*_):
        raise error

    worker, client = agent(handler=fail)
    with pytest.raises(ProviderRequestError) as caught:
        worker.translate(inputs("source"))
    assert caught.value is error
    assert len(client.calls) == 1


@pytest.mark.parametrize("requested", [False, True])
def test_recording_failures_do_not_retry_or_duplicate_paid_calls(requested):
    worker, client = agent({"translations": ["译文"]})
    error = OSError("record storage unavailable")

    def recorder(operation, messages, invoke, validate):
        if requested:
            validate(invoke())
        raise error

    worker._recorder = recorder
    with pytest.raises(OSError) as caught:
        worker.translate(inputs("source"))
    assert caught.value is error
    assert len(client.calls) == int(requested)


def test_interruption_is_not_retried():
    def interrupt(*_):
        raise KeyboardInterrupt

    worker, client = agent(handler=interrupt)
    with pytest.raises(KeyboardInterrupt):
        worker.translate(inputs("source"))
    assert len(client.calls) == 1


def test_invalid_input_drafts_do_not_trigger_model_retries():
    worker, client = agent({"translations": ["稿"]})
    with pytest.raises(PrecisionError):
        worker.synthesize(inputs("source"), [["稿"], [], ["稿"]])
    assert not client.calls


def test_truncation_does_not_add_a_small_budget_or_split_context():
    def truncated(*_):
        raise TruncatedResponseError("provider output ended")

    worker, client = agent(handler=truncated)
    with pytest.raises(PrecisionError, match="truncated"):
        worker.translate(inputs("one", "two"))
    assert len(client.calls) == 3
    assert all(call["max_tokens"] is None for call in client.calls)
    assert all(
        '"sources": ["one", "two"]' in call["messages"][1]["content"] for call in client.calls
    )


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
    assert len(client.calls) == 3


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
