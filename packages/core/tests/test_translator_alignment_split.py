"""Recursive binary-split alignment fallback tests for Translator."""

from __future__ import annotations

import json
import logging
import re

import pytest
from wenyi_core.agents.translator import AlignmentError, Translator
from wenyi_core.config import Config
from wenyi_core.llm.providers.fake import FakeClient


def _config(*, align_retry_limit: int = 1) -> Config:
    return Config.from_dict(
        {
            "language": {"source": "en", "target": "zh"},
            "llm": {"preset": "fake"},
            "pipeline": {"align_retry_limit": align_retry_limit},
        }
    )


def _numbered_sources(user_content: str) -> list[str]:
    return re.findall(r"^\[\d+\] (.*)$", user_content, flags=re.MULTILINE)


def _next_source(user_content: str) -> str:
    marker = "[Following source paragraph] (reference only; do not translate)\n"
    reference = user_content.split(marker, 1)[1].split("\n\n", 1)[0]
    return "" if reference == "(none)" else json.loads(reference)


def _annotation_payload(user_content: str):
    marker = "[Paragraph-specific annotation references] (JSON; only for paragraphs in applies_to, not text to translate)\n"
    payload = user_content.split(marker, 1)[1].split("\n\n[Recent translations]", 1)[0]
    return json.loads(payload)


def test_binary_split_recovers_large_batch_in_two_halves():
    """A 40-paragraph batch failing alignment splits into 20 + 20 (5 total calls vs 43)."""
    sources = [f"Paragraph {i}." for i in range(40)]
    call_sizes: list[int] = []
    next_sources: list[tuple[int, str]] = []

    def handler(messages, tier, json_mode):
        user = messages[-1]["content"]
        batch = _numbered_sources(user)
        call_sizes.append(len(batch))
        next_sources.append((len(batch), _next_source(user)))
        if len(batch) > 20:
            # Simulate count mismatch on oversized batch.
            return json.dumps({"translations": [f"译:{s}" for s in batch[:-1]]}, ensure_ascii=False)
        return json.dumps({"translations": [f"译:{s}" for s in batch]}, ensure_ascii=False)

    client = FakeClient(handler=handler)
    translator = Translator(client, _config(align_retry_limit=2))

    targets, turn, indices = translator.translate_batch_with_transcript(
        sources, next_source="Outside paragraph."
    )

    assert targets == [f"译:{s}" for s in sources]
    assert turn is None
    assert indices is None
    assert call_sizes == [40, 40, 40, 20, 20]
    assert next_sources == [
        (40, "Outside paragraph."),
        (40, "Outside paragraph."),
        (40, "Outside paragraph."),
        (20, "Paragraph 20."),
        (20, "Outside paragraph."),
    ]

    # translate_batch clears cached transcript state when split fallback runs.
    translator.last_batch_turn = [{"role": "user", "content": "stale"}]
    translator.last_batch_indices = [0]
    out = translator.translate_batch(sources, next_source="Outside paragraph.")
    assert out == [f"译:{s}" for s in sources]
    assert translator.last_batch_turn is None
    assert translator.last_batch_indices is None


def test_binary_split_isolates_single_troublesome_paragraph_logarithmically():
    """Only the path containing a poisoned paragraph splits down to length 1."""
    sources = [f"Para {i}" for i in range(16)]
    poisoned = "Para 11"
    call_sizes: list[int] = []

    def handler(messages, tier, json_mode):
        batch = _numbered_sources(messages[-1]["content"])
        call_sizes.append(len(batch))
        if len(batch) > 1 and poisoned in batch:
            return json.dumps({"translations": ["dropped"]}, ensure_ascii=False)
        return json.dumps({"translations": [f"译:{s}" for s in batch]}, ensure_ascii=False)

    translator = Translator(FakeClient(handler=handler), _config(align_retry_limit=1))
    result = translator.translate_batch(sources)

    assert result == [f"译:{s}" for s in sources]
    # DFS left-to-right for Para 11 (in [8..11]):
    # 16 (fail x2) -> [0..7] 8 (ok), [8..15] 8 (fail) -> [8..11] 4 (fail) -> [8..9] 2 (ok),
    # [10..11] 2 (fail) -> [10] 1 (ok), [11] 1 (ok), then [12..15] 4 (ok).
    assert call_sizes == [16, 16, 8, 8, 4, 2, 2, 1, 1, 4]


def test_binary_split_respects_filtered_nonlinguistic_neighbor_as_next_source():
    """Filtered symbols/numbers between or after split halves remain exact next_source neighbors."""
    sources = ["Alpha", "Beta", "42", "Gamma", "Delta", "---"]
    recorded: list[tuple[list[str], str]] = []

    def handler(messages, tier, json_mode):
        user = messages[-1]["content"]
        batch = _numbered_sources(user)
        recorded.append((batch, _next_source(user)))
        if len(batch) > 2:
            return json.dumps({"translations": []})
        return json.dumps({"translations": [f"译:{s}" for s in batch]}, ensure_ascii=False)

    translator = Translator(FakeClient(handler=handler), _config(align_retry_limit=1))
    result = translator.translate_batch(sources, next_source="External next")

    assert result == ["译:Alpha", "译:Beta", "42", "译:Gamma", "译:Delta", "---"]
    assert recorded == [
        (["Alpha", "Beta", "Gamma", "Delta"], "---"),
        (["Alpha", "Beta", "Gamma", "Delta"], "---"),
        (["Alpha", "Beta"], "42"),
        (["Gamma", "Delta"], "---"),
    ]


def test_binary_split_scopes_annotation_contexts_per_half():
    """Each split half receives only its own annotation contexts with re-indexed applies_to."""
    half_payloads: list[list[dict]] = []

    def handler(messages, tier, json_mode):
        user = messages[-1]["content"]
        batch = _numbered_sources(user)
        if len(batch) > 2:
            return json.dumps({"translations": []})
        half_payloads.append(_annotation_payload(user))
        return json.dumps({"translations": [f"译:{s}" for s in batch]}, ensure_ascii=False)

    translator = Translator(FakeClient(handler=handler), _config(align_retry_limit=1))
    result = translator.translate_batch(
        ["First", "Second", "Third", "Fourth"],
        annotation_contexts=[
            [{"target_key": "notes.xhtml#n1", "source": "Shared left note."}],
            [{"target_key": "notes.xhtml#n1", "source": "Shared left note."}],
            [{"target_key": "notes.xhtml#n2", "source": "Third note."}],
            [{"target_key": "notes.xhtml#n3", "source": "Fourth note."}],
        ],
    )

    assert result == ["译:First", "译:Second", "译:Third", "译:Fourth"]
    assert half_payloads == [
        [
            {
                "target_key": "notes.xhtml#n1",
                "source": "Shared left note.",
                "applies_to": [0, 1],
            }
        ],
        [
            {
                "target_key": "notes.xhtml#n2",
                "source": "Third note.",
                "applies_to": [0],
            },
            {
                "target_key": "notes.xhtml#n3",
                "source": "Fourth note.",
                "applies_to": [1],
            },
        ],
    ]


def test_binary_split_calls_translate_one_for_leaves_and_reports_original_failing_index(
    monkeypatch,
):
    """Leaf splits invoke _translate_one and report the original unfiltered paragraph index on error."""
    sources = ["-", "Para zero", "Para one", "99", "Para bad", "Para after"]
    leaf_sources: list[str] = []

    def handler(messages, tier, json_mode):
        batch = _numbered_sources(messages[-1]["content"])
        if "Para bad" in batch:
            return json.dumps({"translations": []})
        return json.dumps({"translations": [f"译:{s}" for s in batch]}, ensure_ascii=False)

    translator = Translator(FakeClient(handler=handler), _config(align_retry_limit=1))
    original_translate_one = translator._translate_one

    def spy_translate_one(source, *args, **kwargs):
        leaf_sources.append(source)
        return original_translate_one(source, *args, **kwargs)

    monkeypatch.setattr(translator, "_translate_one", spy_translate_one)

    with pytest.raises(AlignmentError, match="Single-paragraph fallback failed at paragraph 4"):
        translator.translate_batch(sources)

    # Left half ["Para zero", "Para one"] succeeded as a 2-para batch, so only "Para bad" hit leaf
    # and "Para after" was never attempted after paragraph 4 failed.
    assert leaf_sources == ["Para bad"]


def test_single_paragraph_batch_fails_without_redundant_fallback_call():
    """A 1-paragraph batch exhausting align_retry_limit fails immediately without an extra call."""
    client = FakeClient(handler=lambda *_: json.dumps({"translations": []}))
    translator = Translator(client, _config(align_retry_limit=1))

    with pytest.raises(AlignmentError, match="Single-paragraph fallback failed at paragraph 1"):
        translator.translate_batch(["-", "Only paragraph", "42"])

    assert len(client.calls) == 2


def test_provider_error_during_split_or_leaf_propagates_untouched():
    """Non-AlignmentError provider failures must propagate immediately without further splitting."""
    call_count = 0

    def fail_on_split(messages, tier, json_mode):
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            return json.dumps({"translations": []})
        raise RuntimeError("provider unavailable during split")

    translator = Translator(FakeClient(handler=fail_on_split), _config(align_retry_limit=1))
    with pytest.raises(RuntimeError, match="provider unavailable during split"):
        translator.translate_batch(["p0", "p1", "p2", "p3"])
    assert call_count == 3

    leaf_calls = 0

    def fail_on_leaf(messages, tier, json_mode):
        nonlocal leaf_calls
        leaf_calls += 1
        batch = _numbered_sources(messages[-1]["content"])
        if len(batch) > 1:
            return json.dumps({"translations": []})
        raise RuntimeError("provider unavailable at leaf")

    leaf_translator = Translator(FakeClient(handler=fail_on_leaf), _config(align_retry_limit=1))
    with pytest.raises(RuntimeError, match="provider unavailable at leaf"):
        leaf_translator.translate_batch(["p0", "p1"])
    assert leaf_calls == 3


def test_binary_split_preserves_allow_empty_translations_and_bounds_worst_case_calls():
    """allow_empty_translations flows into splits/leaves, and all-batch-failure stays bounded."""
    sources = [f"Para {i}" for i in range(8)]

    def handler(messages, tier, json_mode):
        batch = _numbered_sources(messages[-1]["content"])
        if len(batch) > 1:
            return json.dumps({"translations": []})
        return json.dumps({"translations": [""]})

    client = FakeClient(handler=handler)
    translator = Translator(client, _config(align_retry_limit=1))

    result = translator.translate_batch(sources, allow_empty_translations=True)

    assert result == [""] * 8
    # Top-level (2) + internal split nodes (N - 2 = 6) + leaves (N = 8) == 16.
    assert len(client.calls) == 16


def test_alignment_warnings_logged_for_retry_split_and_leaf_without_leaking_text(caplog):
    """WARNING logs explain retries, splits, and leaf fallbacks while never logging source text."""
    secret = "SECRET_PRIVATE_BOOK_SENTINEL_XYZ"
    sources = [f"{secret} 0", f"{secret} 1", f"{secret} 2", f"{secret} 3"]

    def recover_at_leaf(messages, tier, json_mode):
        batch = _numbered_sources(messages[-1]["content"])
        if len(batch) > 1 and sources[3] in batch:
            return json.dumps({"translations": []})
        return json.dumps({"translations": [f"译:{i}" for i in range(len(batch))]})

    translator = Translator(FakeClient(handler=recover_at_leaf), _config(align_retry_limit=1))
    with caplog.at_level(logging.DEBUG, logger="wenyi_core.agents.translator"):
        out = translator.translate_batch(sources)

    assert len(out) == 4
    warn_messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    debug_messages = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    messages = [record.getMessage() for record in caplog.records]
    assert any("attempt=1/2" in m and "size=4" in m for m in warn_messages)
    assert any("attempt=2/2" in m and "size=4" in m for m in warn_messages)
    assert any("binary split" in m and "4-paragraph" in m and "2 + 2" in m for m in warn_messages)
    assert any("sub-batch alignment failed" in m and "size=2" in m for m in debug_messages)
    assert any("binary split" in m and "2-paragraph" in m and "1 + 1" in m for m in messages)
    assert any("single-paragraph alignment fallback" in m and "paragraph 2" in m for m in messages)
    assert any("single-paragraph alignment fallback" in m and "paragraph 3" in m for m in messages)
    assert all(secret not in m for m in messages)

    # Also verify leaf failure warning without leaking source text.
    caplog.clear()
    failing_translator = Translator(
        FakeClient(handler=lambda *_: json.dumps({"translations": []})),
        _config(align_retry_limit=0),
    )
    with caplog.at_level(logging.WARNING, logger="wenyi_core.agents.translator"):
        with pytest.raises(AlignmentError, match="Single-paragraph fallback failed at paragraph 0"):
            failing_translator.translate_batch([f"{secret} A", f"{secret} B"])

    fail_messages = [record.getMessage() for record in caplog.records]
    assert any("Single-paragraph fallback failed at paragraph 0" in m for m in fail_messages)
    assert all(secret not in m for m in fail_messages)


def test_retry_success_keeps_transcript_and_does_not_split(caplog):
    """When top-level retry succeeds, transcript is preserved and no split happens."""
    attempts = 0

    def handler(messages, tier, json_mode):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return json.dumps({"translations": []})
        return json.dumps({"translations": ["译0", "译1", "译2"]})

    translator = Translator(FakeClient(handler=handler), _config(align_retry_limit=1))
    with caplog.at_level(logging.WARNING, logger="wenyi_core.agents.translator"):
        targets, turn, indices = translator.translate_batch_with_transcript(["a", "b", "c"])

    assert targets == ["译0", "译1", "译2"]
    assert turn is not None
    assert indices == [0, 1, 2]
    assert attempts == 2
    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 1
    assert "attempt=1/2" in messages[0]
    assert not any("binary split" in m for m in messages)
