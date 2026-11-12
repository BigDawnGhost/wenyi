"""Token-bounded recent translations selected from formal chapter text."""

import pytest
from wenyi_core.ingest.tokens import count_tokens
from wenyi_core.pipeline import context


def test_recent_translations_use_tokens_including_newline_separators():
    targets = ["Older translation.", "Middle translation.", "Newest translation."]
    expected = "\n".join(targets[-2:])

    assert context.render_recent_translations(reversed(targets), count_tokens(expected)) == expected
    assert targets == ["Older translation.", "Middle translation.", "Newest translation."]


def test_many_short_paragraphs_are_not_limited_to_six_or_forty():
    targets = [f"Line {index}" for index in range(60)]
    expected = "\n".join(targets)

    assert context.render_recent_translations(reversed(targets), count_tokens(expected)) == expected


def test_selection_keeps_a_contiguous_suffix_without_skipping_long_paragraphs():
    targets = ["old", "A much longer intervening paragraph. " * 20, "new"]

    assert context.render_recent_translations(reversed(targets), count_tokens("new") + 1) == "new"


@pytest.mark.parametrize("budget", [1, 2, 8, 32])
def test_oversized_newest_paragraph_uses_a_unicode_safe_tail(budget):
    target = "古い文章。中文译文。한국어 문장。🙂 A final sentence." * 10

    rendered = context.render_recent_translations(iter([target]), budget)

    assert count_tokens(rendered) <= budget
    assert target.endswith(rendered)
    assert "\ufffd" not in rendered
    if budget >= count_tokens(" sentence."):
        assert rendered.endswith(" sentence.")


def test_zero_budget_does_not_read_chapter_history():
    def targets():
        raise AssertionError("Disabled context must not read chapter history")
        yield ""

    assert context.render_recent_translations(targets(), 0) == ""


def test_exact_budget_does_not_load_any_older_history():
    def targets():
        yield "Newest paragraph."
        raise AssertionError("The full window must not load another chapter")

    assert context.render_recent_translations(targets(), count_tokens("Newest paragraph.")) == (
        "Newest paragraph."
    )


def test_empty_completed_targets_are_skipped_but_pending_targets_stop_history():
    targets = ["new", "", " \n ", "previous", None, "not contiguous"]

    assert context.render_recent_translations(iter(targets), 512) == "previous\nnew"
