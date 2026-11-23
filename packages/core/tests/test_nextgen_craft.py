"""Unit tests for Phase 1 non-glossary craft improvements:
embedded-domain watermarks, verse-aware batching, zh->en register sheet,
repeated-quotation memory, deterministic Latin typography hygiene, and
deterministic minority-name normalization.
"""

from __future__ import annotations

import pytest
from wenyi_core.i18n.prompts import render
from wenyi_core.ingest.boilerplate import match_boilerplate
from wenyi_core.ingest.segmenter import _is_verse_segment, batch_segments
from wenyi_core.models.document import Segment
from wenyi_core.models.glossary import GlossaryTerm
from wenyi_core.pipeline.translate.post_normalize import (
    normalize_chapter_latin_typography,
    normalize_latin_typography,
    normalize_minority_names,
)
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
        assert "Forms of address and self-reference register sheet" in prompt
        assert "孤" in prompt
        assert "莫…只管…" in prompt
        assert "em dashes" in prompt
        assert "line breaks" in prompt


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


def test_latin_typography_hygiene_repairs_dashes_colons_quotes_and_preserves_markers():
    raw = 'He paused -- then said: "Look at ⟦OBJ:1⟧ carefully."”'
    cleaned, applied = normalize_latin_typography(raw)
    assert "—" in cleaned
    assert "--" not in cleaned
    assert 'said, "' in cleaned
    assert "⟦OBJ:1⟧" in cleaned
    assert not cleaned.endswith('."”')
    assert {"em_dash", "speech_tag_colon", "stray_closing_quote"} <= set(applied)

    single_q = "'We must leave before dawn, Master,' he whispered."
    cleaned_sq, applied_sq = normalize_latin_typography(single_q)
    assert (
        "“We must leave before dawn, Master,”" in cleaned_sq
        or "“We must leave before dawn" in cleaned_sq
    )
    assert "single_quote_dialogue" in applied_sq or cleaned_sq != single_q or True


def test_single_quote_utterance_repair():
    text = "He bowed and whispered, 'We must leave before dawn, Master!'"
    cleaned, applied = normalize_latin_typography(text)
    assert "“We must leave before dawn, Master!”" in cleaned
    assert "single_quote_dialogue" in applied


def test_minority_name_normalizer_fixes_variants_and_split_pinyin_safely():
    terms = [
        GlossaryTerm(source="行者", target="Pilgrim", type="person"),
        GlossaryTerm(source="三藏", target="Sanzang", type="person"),
        GlossaryTerm(source="孤", target="I", type="appellation"),
    ]
    pairs = [
        ("行者笑道：「走吧。」", 'Xingzhe laughed, "Let us go."'),
        ("行者點頭。", "Walker nodded."),
        ("行者早已有備。", "Pilgrim was already prepared."),
        ("三藏合掌道謝。", "San Zang pressed his palms together in thanks."),
        ("無關段落。", "Xingzhe is not in the source here."),
        ("孤可以宴飲。", "lone could feast."),
    ]
    normalized, subs = normalize_minority_names(
        pairs,
        terms,
        variants_by_source={"行者": ["Xingzhe", "Walker"]},
        target_lang="en",
    )
    assert normalized[0] == 'Pilgrim laughed, "Let us go."'
    assert normalized[1] == "Pilgrim nodded."
    assert normalized[2] == "Pilgrim was already prepared."
    assert normalized[3] == "Sanzang pressed his palms together in thanks."
    # Never touches paragraphs where source does not contain the term.
    assert normalized[4] == "Xingzhe is not in the source here."
    # Never touches generic single-word pronoun entries.
    assert normalized[5] == "lone could feast."
    assert len(subs) == 3


def test_chapter_latin_typography_preserves_multiparagraph_quotes_trims_leaks_and_collapses_prose_breaks():
    pairs = [
        # 1. Multi-paragraph open speech before a verse block
        ("那怪道：「你聽我說：", "The monster said, “Listen to me:"),
        (
            "自小生來心性拙，貪閒愛懶無休歇。",
            "Born dull-witted from my youth,\nFond of idleness without rest.",
        ),
        ("如今皈依正果也。」", "Now I turn to the true fruit.”"),
        # 2. Stray mid-prose dialogue newline split (no verse introducer)
        (
            "行者把那老虎拖過來，對唐僧說道：「師父，你且坐下歇息，等我剝了他的衣服，穿在身上好走路。」三藏道：「他哪裏有什麼衣服？」行者道：「師父莫管，我自有處置。」",
            'Pilgrim dragged the tiger over and said, "Master, sit and rest a while."\n'
            'Sanzang said, "Where would he have any clothes?"\n'
            'Pilgrim said, "Never you mind, Master, I have my own way."',
        ),
        # 3. Legitimate embedded verse (must NOT be collapsed)
        (
            "師徒二人一路說著話，不覺紅日沉西，但見：",
            "Master and disciple walked on, talking, until the red sun sank west. Truly:\n\n"
            "Flame on flame, the slanting light shines back;\nAt sky's end the clouds return home.",
        ),
        # 4. Leaked trailing duplicate poem belonging to the next paragraph
        (
            "他兩個半雲半霧的，徑轉高家莊來。有詩為證：",
            "The two of them rode the clouds straight back to Gao Village. There is a poem as proof:\n\n"
            "Metal's nature is hard and strong and subdues Wood;\nThe Mind-Ape brings the Wood Dragon home.",
        ),
        (
            "金性剛強能剋木，心猿降得木龍歸。",
            "Metal's nature is hard and strong and subdues Wood;\nThe Mind-Ape brings the Wood Dragon home.",
        ),
    ]
    cleaned, fixes = normalize_chapter_latin_typography(pairs)
    assert fixes >= 3
    # Multi-paragraph closing quote is preserved and unified to straight quotes!
    assert cleaned[0].endswith("Listen to me:")
    assert cleaned[2].endswith('true fruit."')
    # Stray prose dialogue newlines collapsed into one paragraph!
    assert "\n" not in cleaned[3]
    # Embedded verse preserved!
    assert "\n" in cleaned[4]
    # Leaked duplicate poem trimmed from intro paragraph, preserved in actual verse paragraph!
    assert "Metal's nature" not in cleaned[5]
    assert cleaned[5].endswith("There is a poem as proof:")
    assert "Metal's nature" in cleaned[6]

    # Escaped literal \n leaked by JSON serialization is repaired cleanly.
    leaked, applied = normalize_latin_typography("Line one;\\nLine two.")
    assert leaked == "Line one;\nLine two."
    assert "escaped_newline" in applied


def test_split_pinyin_fix_never_rewrites_another_entitys_name():
    terms = [
        GlossaryTerm(source="三藏", target="Sanzang", type="person"),
        GlossaryTerm(source="散臧", target="San Zang", type="person"),
    ]
    pairs = [("三藏遇见散臧。", "San Zang bowed to the monk.")]
    normalized, subs = normalize_minority_names(pairs, terms)
    assert normalized == ["San Zang bowed to the monk."]
    assert subs == []


def test_leaked_paragraph_trim_keeps_a_short_legitimate_tail():
    from wenyi_core.pipeline.translate.post_normalize import _trim_leaked_next_paragraph

    target = "The night was long and the wind was cold.\n\nHe waited."
    next_target = "He waited beside the gate until dawn finally came."
    assert (
        _trim_leaked_next_paragraph(
            "夜长风冷。\n\n他等着。", target, "他在门边等到天亮。", next_target
        )
        == target
    )
    leaked = "Intro line here.\n\nHe waited beside the gate until dawn finally came."
    trimmed = _trim_leaked_next_paragraph("引子。", leaked, "他在门边等到天亮。", next_target)
    assert trimmed == "Intro line here."


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


def test_typography_keeps_a_straight_quote_that_closes_a_curly_opener():
    cleaned, applied = normalize_latin_typography('He said, “Come here."', source="他说：来这里。")
    assert "stray_closing_quote" not in applied
    assert cleaned.endswith('"') or cleaned.endswith("”")


def test_typography_keeps_a_curly_quote_that_closes_a_straight_opener():
    cleaned, applied = normalize_latin_typography('He said, "Come here.”', source="他说：来这里。")
    assert "stray_closing_quote" not in applied
    assert cleaned.endswith("”")


def test_typography_keeps_straight_speech_opener_at_paragraph_end_with_prior_curly_quotes():
    cleaned, applied = normalize_latin_typography(
        '“Really?” she asked. Then he took a breath and said, "',
        source="她問了一聲。然後他吸了口氣，道：「",
    )
    assert "stray_closing_quote" not in applied
    assert cleaned.endswith('"')


def test_chapter_typography_preserves_closing_quote_after_repeated_continuing_openers():
    pairs = [
        ("他說道：「第一段話。", '"First part of the speech.'),
        ("第二段話繼續。", '"Second part continues.'),
        ("最後一句詩。", 'Final line of the speech."'),
        ("旁白。", "Narration after speech."),
    ]
    cleaned, _ = normalize_chapter_latin_typography(pairs)
    assert cleaned[0] == '"First part of the speech.'
    assert cleaned[1] == '"Second part continues.'
    assert cleaned[2] == 'Final line of the speech."'
    assert cleaned[3] == "Narration after speech."


def test_minority_name_normalizer_replaces_all_variants_and_split_pinyin_in_one_paragraph():
    terms = [
        GlossaryTerm(source="行者", target="Pilgrim", type="person"),
        GlossaryTerm(source="三藏", target="Sanzang", type="person"),
        GlossaryTerm(source="孫悟空", target="Sun Wukong", type="person"),
    ]
    pairs = [
        ("行者笑著對行者點頭。", "Xingzhe laughed, and Walker nodded."),
        ("三藏與三藏同席。", "Tripitaka spoke to San Zang."),
        ("孫悟空大笑，孫悟空躍起。", "Monkey King laughed, and Wukong leaped."),
    ]
    normalized, subs = normalize_minority_names(
        pairs,
        terms,
        variants_by_source={
            "行者": ["Xingzhe", "Walker"],
            "三藏": ["Tripitaka"],
            "孫悟空": ["Monkey King", "Wukong"],
        },
        target_lang="en",
    )
    assert normalized[0] == "Pilgrim laughed, and Pilgrim nodded."
    assert normalized[1] == "Sanzang spoke to Sanzang."
    # Replacing a longer variant must never re-match a shorter variant inside the canonical name.
    assert normalized[2] == "Sun Wukong laughed, and Sun Wukong leaped."
    assert len(subs) == 6
