"""Lossless, balanced token splitting with the shared real tokenizer."""

import pytest
from wenyi_core.ingest.tokens import count_tokens, split_balanced_tokens


@pytest.mark.parametrize(
    ("source", "budget", "expected"),
    [
        ("", 8, []),
        (" x" * 7, 8, [7]),
        (" x" * 8, 8, [8]),
        (" x" * 9, 8, [4, 5]),
        (" x" * 16, 8, [8, 8]),
        (" x" * 17, 8, [5, 6, 6]),
        (" x" * 7, 1, [1] * 7),
        ("中" * 3 + "🙂" + "文" * 4, 8, [5, 4]),
        ("中" * 7 + "🙂" + "文" * 7, 8, [5, 5, 6]),
    ],
)
def test_balanced_split_uses_fewest_even_chunks(source, budget, expected):
    chunks = split_balanced_tokens(source, budget)
    assert "".join(chunks) == source
    assert [count_tokens(chunk) for chunk in chunks] == expected


@pytest.mark.parametrize(
    "source",
    [
        "中文段落与标点，保留原始内容。\n" * 17,
        "日本語の文章を順番どおりに保持する。\n" * 17,
        "Привет, мир! مرحبا بالعالم!\n" * 17,
        "🙂👩🏽‍💻🚀e\u0301\n" * 17,
        " \t\r\nHello 中文🙂 world!\r\n" * 17,
    ],
)
@pytest.mark.parametrize("budget", [8, 17, 32])
def test_balanced_split_preserves_unicode_and_whitespace(source, budget):
    chunks = split_balanced_tokens(source, budget)
    assert "".join(chunks) == source
    assert all(chunk and count_tokens(chunk) <= budget for chunk in chunks)


@pytest.mark.parametrize("budget", [0, -1])
def test_balanced_split_rejects_nonpositive_budget(budget):
    with pytest.raises(ValueError, match="positive"):
        split_balanced_tokens("text", budget)


def test_balanced_split_rejects_budget_smaller_than_one_codepoint():
    assert count_tokens("🙂") > 1
    with pytest.raises(ValueError, match="Unicode"):
        split_balanced_tokens("🙂", 1)
