"""Unit tests for Phase 1 non-glossary craft improvements:
embedded-domain watermarks, verse-aware batching, zh->en register sheet,
repeated-quotation memory, and leaked-escape repair.
"""

from __future__ import annotations

import pytest
from wenyi_core.i18n.prompts import render
from wenyi_core.ingest.boilerplate import match_boilerplate
from wenyi_core.ingest.segmenter import _is_verse_segment, batch_segments
from wenyi_core.models.document import Segment
from wenyi_core.pipeline.translate.post_normalize import repair_escaped_newlines
from wenyi_core.pipeline.translate.quote_memory import QuoteMemory


@pytest.mark.parametrize(
    "text",
    [
        "好书尽在www.qinkan.net",
        "Txt,Epub,Mobi www.qinkan.net",
        "更多精彩小说下载 https://www.example.net/book",
        "www.qinkan.net 全文免费阅读",
        "免费下载www.foo.com全文阅读",
        "【阅读 www.foo.com 下载】",
        "好书尽在www.qinkan.net，第一时间更新",
        "更多精彩，请访问www.xxx.com",
        "https://www.foo.com/book全文免费阅读",
        "【阅读https://www.foo.com/book下载】",
        "——好书尽在www.qinkan.net，第一时间更新",
        "★好书尽在www.qinkan.net，免费阅读",
    ],
)
def test_boilerplate_catches_embedded_domain_watermarks(text: str):
    assert match_boilerplate(text) == "builtin:zh_site_watermark"


@pytest.mark.parametrize(
    "text",
    [
        "他在浏览器里打开了 www.example.com，仔细查看着上面的新闻报道。",
        "She typed https://example.org into the terminal and waited.",
        "他打开www.example.com阅读那封信，随后关掉了电脑。",
        "小说里提到的网址是www.example.com。",
        '他打开www.baidu.com，搜索"免费小说"。',
        "Get the free EPUB or PDF at https://example.org.",
        "他更新了小说里的设定，网址www.foo.com",
        "更多细节请看他的阅读笔记：https://foo.org/x",
        "PDF version at https://example.org.",
        "https://example.org 值得仔细阅读。",
    ],
)
def test_boilerplate_ignores_normal_prose_with_urls(text: str):
    assert match_boilerplate(text) is None


def test_zh_en_and_zh_hant_en_register_sheet_and_verse_policy_render():
    for src in ("zh", "zh-Hant"):
        prompt = render("translator_system", src=src, tgt="en")
        assert "Forms of address, register and fidelity guidance (zh→en)" in prompt
        assert "孤" in prompt
        assert "the sir" in prompt
        assert "line breaks" in prompt
        # Keep the zh->en pair sheet compact and free of book-specific answer rules.
        assert "two hours" not in prompt
        assert "three to five" not in prompt
        assert "Xin Qiji" not in prompt


def test_english_target_rules_carry_no_source_specific_or_regional_style():
    """Rules for every English target must not assume a Chinese source or US house style."""
    for src in ("fr", "ja", "de"):
        prompt = render("translator_system", src=src, tgt="en")
        for leaked in ("couplet", "tune title", "American", "em dashes", "said:"):
            assert leaked not in prompt


def test_verse_aware_batching_keeps_couplet_poem_intact():
    prose = [
        Segment(
            index=i, source="这是一段很长的正常叙事文字，描述山路蜿蜒曲折、行人缓缓前行的景象。" * 8
        )
        for i in range(6)
    ]
    poem = [Segment(index=6 + i, source="此是上寶沁金鈀，須彌靈吉煉成它。") for i in range(10)]
    all_segs = prose + poem
    batches = batch_segments(all_segs, max_tokens=600)
    assert all(_is_verse_segment(seg.source) for seg in poem)
    # Find which batch(es) contain the poem segments: all 10 couplets should stay in one batch.
    poem_batches = [idx for idx, b in enumerate(batches) if any(seg.index >= 6 for seg in b)]
    assert len(poem_batches) == 1
    assert sum(len(b) for b in batches) == len(all_segs)


def test_quote_memory_detects_repeated_verse_and_formats_callback_reference():
    lyric = "妾拟将身嫁与，纵被无情弃，不能羞。"
    chapters = [
        (0, ["普通段落。", f"纸上写着：「{lyric}」", "又一段话。"]),
        (1, ["隔了许多段落。", f"他再看那句「{lyric}」，心中微动。"]),
    ]
    mem = QuoteMemory.from_chapters(chapters)
    assert mem.repeated_count == 1
    mem.observe(
        (0, 1),
        chapters[0][1][1],
        'On the paper was written: "I would give myself to you; cast aside without mercy, I feel no shame."',
    )
    block = mem.format_prompt_block([chapters[1][1][1]])
    assert "Repeated quotation reference" in block
    assert lyric in block
    assert "I feel no shame" in block


def test_quote_memory_keeps_meaning_bearing_punctuation_in_keys():
    memory = QuoteMemory.from_chapters(
        [(0, ["We'll go to the old house.", "Well, go to the old house."])]
    )
    assert memory.repeated_count == 0


def test_quote_memory_skips_inner_quotes_without_a_target_quote():
    quote = "「天下大势，分久必合，合久必分」"
    memory = QuoteMemory.from_chapters(
        [(0, [f"他说{quote}，然后沉默了很久很久。", f"她也说{quote}，众人都笑了起来。"])]
    )
    assert memory.repeated_count == 1
    # The target uses single quotes, so no quote can be identified; narration is not stored.
    memory.observe(
        (0, 0),
        f"他说{quote}，然后沉默了很久很久。",
        "He said, 'The empire long divided must unite,' then fell silent.",
    )
    assert memory.references_for_batch([f"她也说{quote}，众人都笑了起来。"]) == []


def test_escaped_newline_repair_keeps_code_latex_and_paths():
    inline = r"Use `\n` for a line break -- or `--verbose` for logs."
    assert repair_escaped_newlines("原文。", inline) == inline
    fenced = 'Example:\n```python\nprint("a\\nb")\n```'
    assert repair_escaped_newlines("原文。", fenced) == fenced
    nested = '````markdown\n```python\nprint("a\\nb") # --flag\n```\n````'
    assert repair_escaped_newlines("原文。", nested) == nested
    unclosed = '```python # note `x`\nprint("a\\nb")'
    assert repair_escaped_newlines("原文。", unclosed) == unclosed
    latex_math = (
        r"When $x \neq y$, $\alpha,\nu$, \(\alpha,\nu\), $$a,\nabla b$$, f(x)\neq 0, "
        r"and $A \nRightarrow B$."
    )
    assert repair_escaped_newlines("原文。", latex_math) == latex_math
    for bare_protected in (
        r"He stopped.\neq x",
        r"He stopped.\ne x",
        r"He stopped.\nu x",
        r"He stopped.\nabla x",
        r"He stopped.\not x",
        r"He stopped.\nRightarrow x",
        r"He stopped.\nVDash x",
        r"Let a,\nu be real, b,\neq c, p,\neg q, a,\notin S, S,\ni x, a,\napprox b, a,\nolimits.",
        r"See .\new folder for notes.",
    ):
        assert repair_escaped_newlines("原文。", bare_protected) == bare_protected


def test_escaped_newline_repair_fixes_leaked_breaks_in_any_script():
    assert repair_escaped_newlines("他停下。次日早晨。", r"He stopped.\nNext morning.") == (
        "He stopped.\nNext morning."
    )
    assert repair_escaped_newlines("原文。", r"He stopped.\n\nnext line.") == (
        "He stopped.\n\nnext line."
    )
    assert repair_escaped_newlines("原文。", r"At dusk,\nu-boats arrived.") == (
        "At dusk,\nu-boats arrived."
    )
    assert repair_escaped_newlines("Er schwieg.", r"他沉默了。\n第二天早上。") == (
        "他沉默了。\n第二天早上。"
    )
    assert repair_escaped_newlines("Он молчал.", r"Он молчал.\nУтром он ушёл.") == (
        "Он молчал.\nУтром он ушёл."
    )


def test_escaped_newline_repair_keeps_spaced_escape_notation_in_prose():
    # A technical book can name the escape in words in the source and write it in the target.
    target = "Символ новой строки обозначают как \\n."
    assert repair_escaped_newlines("改行文字を表す記号。", target) == target


@pytest.mark.parametrize(
    "target",
    [r"Use \n\n to separate paragraphs.", r"Line breaks are written as \n\n\n here."],
)
def test_escaped_newline_repair_never_half_converts_a_spaced_escape_run(target: str):
    assert repair_escaped_newlines("改行を二つ使う。", target) == target


def test_escaped_newline_repair_keeps_literal_escapes_the_source_also_has():
    # A programming book explaining escape sequences: the literal \n is content, not a leak.
    source = r"Write \n to start a new line."
    target = r"Écrivez \n pour commencer une nouvelle ligne."
    assert repair_escaped_newlines(source, target) == target


@pytest.mark.parametrize(
    "target",
    [
        "Run git checkout --detach before proceeding.",
        "— Olá — disse ele. — Tudo bem?",
        '„Komm her“, sagte er. Das Brett ist 12" lang.',
        "‘You cannot be serious,’ she said.",
        'The agency published its report: "Water Quality in 2020".',
        "MARTIN: “We should go now, before it gets dark.”\nELLA: “Not yet, I want to see the end.”",
        "Premise:\nWhen the price rises, demand falls.",
    ],
)
def test_post_translation_hygiene_never_rewrites_punctuation_or_layout(target: str):
    """House styles differ by language and genre; hygiene must leave them as drafted."""
    assert repair_escaped_newlines("原文。", target) == target
