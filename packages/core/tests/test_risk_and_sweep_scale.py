"""Full-book scale regressions for the structural batch pre-screener and consistency drift miner.

Covers glossary miss matching (case-insensitivity, conservative English plurals, CJK numeral
boundary isolation, covering-term deduplication, and batch-level pre-filtering), embedded verse
precision against prose dialogue verbs, concise-reasoning sparse-editor prompts, and English
inflection filtering for Book Bible-watched titles in ``detect_mid_frequency_drift``.
"""

from __future__ import annotations

from wenyi_core.agents.batch_risk import (
    _target_contains_term,
    assess_batch_risk,
    has_embedded_verse,
)
from wenyi_core.models.document import Chapter, Segment
from wenyi_core.models.glossary import GlossaryTerm
from wenyi_core.pipeline.translate.consistency_sweep import (
    _same_en_lemma,
    detect_mid_frequency_drift,
)

_NAME_TERMS = [
    GlossaryTerm(source="威尔", target="Will", type="person"),
    GlossaryTerm(source="霍普", target="Hope", type="person"),
    GlossaryTerm(source="怀特", target="White", type="person"),
    GlossaryTerm(source="京襄", target="Jingxiang", type="place"),
]


def _codes(report) -> set[tuple[int, str]]:
    return {(a.index, a.code) for a in report.alerts}


# --- batch_risk: glossary_miss ----------------------------------------------------------


def test_glossary_miss_matching_stays_case_insensitive():
    assert _target_contains_term("will you go?", "Will", target_lang="en")
    assert _target_contains_term("Will you go?", "Will", target_lang="en")
    report = assess_batch_risk(
        ["威尔，你会去吗？"],
        ["Will, will you go?"],
        source_lang="zh",
        target_lang="en",
        glossary_terms=_NAME_TERMS,
    )
    assert not any(a.code == "glossary_miss" for a in report.alerts)
    miss = assess_batch_risk(
        ["威尔推门而入。"],
        ["Bill pushed the door open."],
        source_lang="zh",
        target_lang="en",
        glossary_terms=_NAME_TERMS,
    )
    assert any(a.code == "glossary_miss" for a in miss.alerts)


# --- batch_risk: conservative English plural matching -------------------------------------


def test_target_contains_term_english_plurals():
    # Common English -s/-es/-ies plural inflection of the final word, English targets only.
    assert _target_contains_term("The Federation sols gleamed.", "Federation sol", target_lang="en")
    assert _target_contains_term("Two Churches stood.", "Church", target_lang="en")
    assert _target_contains_term("The Monarchs gathered.", "Monarch", target_lang="en")
    assert _target_contains_term("The Patriarchs of the clan met.", "Patriarch", target_lang="en")
    assert _target_contains_term("The Eunuchs bowed.", "Eunuch", target_lang="en")
    assert _target_contains_term("Across three Epochs.", "Epoch", target_lang="en")
    assert _target_contains_term("Three Mechs deployed.", "Mech", target_lang="en")
    assert _target_contains_term("The Companies merged.", "Company", target_lang="en")
    assert _target_contains_term("Three Keys jingled.", "Key", target_lang="en")
    assert _target_contains_term("The Cities burned.", "City", target_lang="en")
    assert _target_contains_term("The Empires collapsed.", "Empire", target_lang="en")
    # Singulars, possessives and article-stripped plurals keep matching.
    assert _target_contains_term("The Empire collapsed.", "Empire", target_lang="en")
    assert _target_contains_term("The Empire's banners fell.", "Empire", target_lang="en")
    assert _target_contains_term("Companies arrived.", "the Company", target_lang="en")
    # allow_plural=False protects short person names from absorbing different words/names.
    assert not _target_contains_term("Hans spoke.", "Han", target_lang="en", allow_plural=False)
    assert not _target_contains_term(
        "Amid the chaos of battle.", "Chao", target_lang="en", allow_plural=False
    )
    # Word boundaries stay intact: never match inside a longer word.
    assert not _target_contains_term("Anna smiled.", "Ann", target_lang="en")
    assert not _target_contains_term("The Ward boundary held.", "War", target_lang="en")
    assert not _target_contains_term("The Kingdom fell.", "King", target_lang="en")
    # Other Latin languages keep exact matching: French plurals are not accepted.
    assert not _target_contains_term("Les rois partirent.", "Roi", target_lang="fr")
    assert _target_contains_term("Les rois partirent.", "Rois", target_lang="fr")
    # CJK targets keep substring matching.
    assert _target_contains_term("他望向京襄城。", "京襄", target_lang="zh")


def test_glossary_miss_skips_english_plurals_for_person_terms_only():
    terms = [
        GlossaryTerm(source="韩兄", target="Han", type="person"),
        GlossaryTerm(source="晁公", target="Chao", type="person"),
        GlossaryTerm(source="联邦索尔", target="Federation sol", type="term"),
    ]
    report = assess_batch_risk(
        [
            "韩兄推门而入。",
            "晁公沉声道。",
            "桌上放着十枚联邦索尔。",
        ],
        [
            "Hans pushed the door open.",
            "Amid the chaos, he spoke quietly.",
            "Ten Federation sols lay on the table.",
        ],
        source_lang="zh",
        target_lang="en",
        glossary_terms=terms,
    )
    codes = _codes(report)
    assert (0, "glossary_miss") in codes
    assert (1, "glossary_miss") in codes
    assert (2, "glossary_miss") not in codes


def test_consistency_sweep_accepts_term_plurals_but_flags_person_name_drift():
    from wenyi_core.pipeline.translate.consistency_sweep import ConsistencySweepService

    seen_calls: list[tuple[list[str], list[str]]] = []

    class _RecordingEditor:
        def edit_batch(self, sources, drafts, *, glossary_terms=None, style=""):
            seen_calls.append((list(sources), list(drafts)))

            class _Res:
                failed = False
                patched_indices: tuple[int, ...] = ()
                targets = tuple(drafts)

            return _Res()

        def adjudicate_drift(self, clusters, *, style="", conventions=""):
            return {}

    terms = [
        GlossaryTerm(source="联邦索尔", target="Federation sol", type="term"),
        GlossaryTerm(source="韩兄", target="Han", type="person"),
    ]
    chapters = {
        0: Chapter(
            index=0,
            title="第一章",
            segments=[
                Segment(
                    index=0,
                    source="他掏出十枚联邦索尔。",
                    target="He took out ten Federation sols.",
                ),
                Segment(index=1, source="韩兄点了点头。", target="Hans nodded."),
            ],
        ),
        1: Chapter(
            index=1,
            title="第二章",
            segments=[
                Segment(index=0, source="夜幕降临。", target="Night fell."),
            ],
        ),
    }

    class _FakeStore:
        def load_chapter(self, idx: int) -> Chapter:
            return chapters[idx]

        def save_chapter(self, ch: Chapter) -> None:
            chapters[ch.index] = ch

        def open_conflicts(self) -> list[dict]:
            return []

    sweeper = ConsistencySweepService(_RecordingEditor(), source_lang="zh", target_lang="en")
    sweeper.run(_FakeStore(), chapter_indices=[0, 1], terms=terms)
    assert len(seen_calls) == 1
    assert seen_calls[0][0] == ["韩兄点了点头。"]
    assert seen_calls[0][1] == ["Hans nodded."]


def test_glossary_generic_target_words_returns_stable_cached_frozenset():
    from wenyi_core.agents.batch_risk import _is_generic
    from wenyi_core.i18n.languages import glossary_generic_target_words

    words_a = glossary_generic_target_words("en")
    words_b = glossary_generic_target_words("en")
    assert words_a is words_b
    _is_generic.cache_clear()
    assert _is_generic("Your Excellency", words_a) is True
    assert _is_generic("Your Excellency", words_b) is True
    assert _is_generic.cache_info().hits >= 1


def test_assess_batch_risk_handles_shorter_drafts_without_index_error():
    terms = [GlossaryTerm(source="黑石城", target="Blackstone City", type="place")]
    report = assess_batch_risk(
        ["黑石城的城门紧闭。", "第二段没有对应译文。"],
        ["The gates of the dark fortress were shut."],
        source_lang="zh",
        target_lang="en",
        glossary_terms=terms,
    )
    assert (0, "glossary_miss") in _codes(report)


# --- batch_risk: batch-level pre-filter equivalence ----------------------------------------


def test_batch_prefilter_catches_glossary_miss_on_first_middle_last_paragraphs():
    terms = [GlossaryTerm(source="黑石城", target="Blackstone City", type="place")]
    sources = [
        "黑石城的城门紧闭。",
        "士兵们在城墙上巡逻。",
        "黑石城的粮草告急。",
        "雨下了整整一夜。",
        "黑石城的守军投降了。",
    ]
    drafts = [
        "The gates of the dark fortress were shut.",
        "Soldiers patrolled the walls.",
        "Supplies ran low in the dark fortress.",
        "Rain fell all night long.",
        "The garrison of the dark fortress surrendered.",
    ]
    report = assess_batch_risk(
        sources, drafts, source_lang="zh", target_lang="en", glossary_terms=terms
    )
    codes = _codes(report)
    for idx in (0, 2, 4):
        assert (idx, "glossary_miss") in codes
    assert not any(idx in {1, 3} for idx, code in codes if code == "glossary_miss")


def test_batch_prefilter_keeps_ascii_word_boundaries_at_paragraph_edges():
    terms = [GlossaryTerm(source="Ann", target="安姑娘", type="person")]
    sources = [
        "Ann opened the gate at dawn.",
        "Anna waved from the window.",
        "Nobody greeted Ann.",
        "The guard said nothing.",
        "Ann closed the gate.",
    ]
    drafts = [
        "她打开了大门。",
        "她在窗边挥手。",
        "没有人上前招呼。",
        "卫兵一言不发。",
        "大门关上了。",
    ]
    report = assess_batch_risk(
        sources, drafts, source_lang="en", target_lang="zh", glossary_terms=terms
    )
    # "\n" is non-word, so terms at paragraph starts/ends stay matches in the joined
    # batch text, while "Ann" never leaks into "Anna".
    assert _codes(report) == {(0, "glossary_miss"), (2, "glossary_miss"), (4, "glossary_miss")}


def test_glossary_miss_covering_term_dedup_survives_batch_prefilter():
    terms = [
        GlossaryTerm(source="石城", target="Stone City", type="place"),
        GlossaryTerm(source="黑石城", target="Blackstone City", type="place"),
    ]
    sources = ["他们抵达黑石城。", "大军进驻黑石城。"]
    drafts = ["They reached Blackstone City.", "The army entered Blackstone City."]
    report = assess_batch_risk(
        sources, drafts, source_lang="zh", target_lang="en", glossary_terms=terms
    )
    # The shorter source 石城 matches too, but the longer covering term renders correctly,
    # so neither paragraph may raise a glossary_miss alert.
    assert not any(a.code == "glossary_miss" for a in report.alerts)


def test_assess_batch_risk_keeps_hits_at_full_book_scale():
    terms = [
        GlossaryTerm(source=f"虚指{i:03d}", target=f"Nowhere{i:03d}", type="place")
        for i in range(240)
    ]
    terms[10] = GlossaryTerm(source="黑石城", target="Blackstone City", type="place")
    sources, drafts = [], []
    for i in range(200):
        sources.append(f"第{i}段：将士们在城外列阵，旌旗猎猎作响。")
        drafts.append(f"Line {i}: the soldiers formed up outside the walls, banners snapping.")
    sources[0] = "黑石城的城门紧闭，将士们严阵以待。"
    drafts[0] = "The gates of the dark fortress were shut, the soldiers in readiness."
    sources[100] = "大军在黑石城下扎营，粮草充足。"
    drafts[100] = "The army camped beneath the dark fortress, supplies plentiful."
    sources[199] = "黑石城的守军开城投降。"
    drafts[199] = "The garrison of the dark fortress surrendered."
    report = assess_batch_risk(
        sources, drafts, source_lang="zh", target_lang="en", glossary_terms=terms
    )
    codes = _codes(report)
    # 241 terms against 200 paragraphs: the batch pre-filter must keep every real hit on
    # the first, middle and last paragraphs while skipping the absent filler terms.
    for idx in (0, 100, 199):
        assert (idx, "glossary_miss") in codes
    assert len(codes) == 3


def test_embedded_verse_excludes_prose_yin_dao_dialogue_tags_but_keeps_recitations():
    # Prose dialogue verbs ending in 吟道 must never trigger embedded_verse.
    for prose in (
        "夏洛特沉吟道：“难道是他无意中走入了迷失地带？”",
        "女仆小姐此时笑吟吟道：“秦小姐难道又忘记了？”",
        "伤员痛苦地呻吟道：“别碰我的伤口。”",
    ):
        assert has_embedded_verse(prose, source_lang="zh") is False

    # Genuine verse recitations still trigger embedded_verse.
    for verse in (
        "林远来到青云楼前，吟道：“大漠孤烟直，长河落日圆。”",
        "他情不自禁便轻声吟道：“含吐缃缥之上，翩跹樽俎之侧……”",
        "只听见那青年低声吟道：“别来春半，触目柔肠断。砌下落梅如雪乱。”",
    ):
        assert has_embedded_verse(verse, source_lang="zh") is True


def test_zh_numeral_glossary_name_ignores_chapter_headings_and_counts():
    terms = [GlossaryTerm(source="十一", target="Eleven", type="person")]
    sources = [
        "第七十一章 我要做英雄",
        "第十一章 第一晚",
        "第十一回 英雄出世",
        "十一回 英雄出世",
        "十一月十一日十一点，他买回十一个苹果。",
        "十一人齐声答道，两人大战十一回合，他去了十一回才找到。十一回过去了，他仍未成功。",
        "十一笑了笑，推门走了进来。",
        # 十一回 is ambiguous (the character turns, or "eleven times"); counts win, so no alert.
        "十一回头望去。",
    ]
    drafts = [
        "Chapter 71: I Want to Be a Hero",
        "Chapter 11: The First Night",
        "Chapter 11: A Hero Appears",
        "Episode 11: A Hero Appears",
        "At eleven o'clock on November 11, he bought eleven apples.",
        "All 11 people answered in unison; the two fought 11 rounds, he went 11 times before finding it, and 11 times passed without success.",
        "Shi Yi smiled and pushed the door open.",
        "Shi Yi looked back.",
    ]
    report = assess_batch_risk(
        sources, drafts, source_lang="zh", target_lang="en", glossary_terms=terms
    )
    assert _codes(report) == {(6, "glossary_miss")}


def test_modern_prose_produces_zero_structural_false_positives():
    sources = [
        "夏洛特沉吟道：“他与她近万年的时光没有见面了。”",
        "不要说是小舢板了，就算是个水性好的人，也未必敢闯入。",
        "只要不是拆了这个城堡，就没有问题了。",
        "这不可能是天煞孤星，也不是孤胆英雄。",
        "“我看你，故意制造事端，好让我们内讧？”",
        "她比那位霸道的大姨妈也不差多少。",
        "许久之后，他才缓缓地睁开了眼睛。",
    ]
    drafts = [
        'Charlotte mused, "He and she had not met for nearly ten thousand years."',
        "Never mind a small rowboat—even a strong swimmer might not dare enter.",
        "As long as you don't tear down this castle, there's no problem.",
        "This couldn't be the lone star of calamity, nor a lone hero.",
        '"I see you are deliberately causing trouble to make us fight?"',
        "She's no weaker than that overbearing Aunt Lucia.",
        "A long while later, he slowly opened his eyes.",
    ]
    report = assess_batch_risk(sources, drafts, source_lang="zh", target_lang="en")
    assert report.alerts == ()


def test_sparse_editor_system_prompt_includes_concise_reasoning_rule():
    from wenyi_core.agents.sparse_editor import SparseSemanticEditor
    from wenyi_core.config import Config
    from wenyi_core.llm.configuration import LLMConfig
    from wenyi_core.llm.providers.fake import FakeClient

    seen_systems: list[str] = []

    def handler(messages, op, json_mode):
        seen_systems.append(messages[0]["content"])
        return '{"patches": []}'

    cfg = Config(source_lang="zh", target_lang="en", llm=LLMConfig(preset="fake"))
    fake = FakeClient(handler)
    editor = SparseSemanticEditor(fake, cfg)
    res = editor.edit_batch(["天气很好。"], ["The weather was fine."])
    assert not res.failed
    assert seen_systems
    assert "NEVER enumerate, summarize, or check off clean paragraphs" in seen_systems[0]
    assert fake.calls[-1]["thinking_boost"] == 0


# --- consistency_sweep: drift mining ----------------------------------------------------


def test_same_en_lemma_collapses_inflections_only():
    same = [
        ("need", "needed"),
        ("needing", "need"),
        ("called", "calling"),
        ("needed", "needing"),
        ("left", "leaving"),
        ("told", "telling"),
        ("made", "making"),
        ("stopped", "stopping"),
        ("leave", "left"),
        ("tell", "told"),
        ("call", "called"),
        ("hope", "hoped"),
        ("make", "making"),
        ("stop", "stopped"),
        ("carry", "carried"),
        ("think", "thought"),
        ("rise", "rose"),
        ("show", "showed"),
        ("enemy", "enemie"),
        ("princess", "princesse"),
        ("church", "churche"),
        ("taught", "teache"),
        ("caught", "catche"),
    ]
    for a, b in same:
        assert _same_en_lemma(a, b), (a, b)
    for a, b in [
        ("sovereign", "king"),
        ("adviser", "retainer"),
        ("king", "kingdom"),
        ("chance", "opportunity"),
        ("bear", "beard"),
        ("boar", "board"),
        ("bran", "brand"),
        ("hunt", "hunter"),
        ("kill", "killer"),
        ("office", "officer"),
        ("prison", "prisoner"),
        ("command", "commander"),
        ("rule", "ruler"),
    ]:
        assert not _same_en_lemma(a, b), (a, b)


def _filler(count: int, start: int = 0) -> list[Segment]:
    # Non-CJK sources contribute paragraphs (base rates) without contributing n-grams.
    return [
        Segment(
            index=start + i, source=f"-- {start + i} --", target=f"Filler line number {start + i}."
        )
        for i in range(count)
    ]


def _chapters(rows_by_chapter: list[list[tuple[str, str]]], filler: int = 6) -> list[Chapter]:
    chapters = []
    for ci, rows in enumerate(rows_by_chapter):
        segs = [Segment(index=i, source=s, target=t) for i, (s, t) in enumerate(rows)]
        segs += _filler(filler, start=len(segs))
        chapters.append(Chapter(index=ci, title=f"Ch {ci}", segments=segs))
    return chapters


def test_drift_ignores_inflection_splits_but_keeps_real_synonym_splits():
    chapters = _chapters(
        [
            [("征召", "Summon them."), ("征召", "Summon them!")]
            + [("幕僚", "The adviser bowed."), ("幕僚", "The adviser bowed.")],
            [("征召", "Summoned them."), ("征召", "Summoned them!")]
            + [("幕僚", "The retainer bowed."), ("幕僚", "The retainer bowed.")],
        ]
    )
    sources = {
        c.source
        for c in detect_mid_frequency_drift(
            chapters,
            source_lang="zh",
            target_lang="en",
            extra_watch_sources={"征召": (), "幕僚": ()},
        )
    }
    assert "征召" not in sources
    assert "幕僚" in sources


def test_drift_only_checks_book_bible_watched_sources():
    """Unwatched n-grams are never mined, so no per-language vocabulary lists are needed."""
    ch0, ch1 = _split_rows("幕僚", "adviser", "retainer", 8)
    chapters = _chapters([ch0, ch1])
    assert detect_mid_frequency_drift(chapters, source_lang="zh", target_lang="en") == []
    # The same split is reported once the Book Bible flags the source as divergent, for any
    # source language.
    for lang in ("zh", "ja"):
        watched = detect_mid_frequency_drift(
            chapters,
            source_lang=lang,
            target_lang="en",
            extra_watch_sources={"幕僚": ("the adviser", "the retainer")},
        )
        assert [c.source for c in watched] == ["幕僚"]


def _split_rows(
    src: str, a: str, b: str, n: int
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    half = n // 2
    return (
        [(src, f"Then the {a} waited.") for _ in range(half)],
        [(src, f"Then the {b} waited.") for _ in range(n - half)],
    )


def test_drift_ranking_prefers_title_signal_over_raw_frequency():
    freq0, freq1 = _split_rows("棋局", "board", "match", 16)
    title0 = [("宰辅", "Then, the Chancellor waited.")] * 2
    title1 = [("宰辅", "Then, the Minister waited.")] * 2
    watch0 = [("国主", "The sovereign waited.")] * 2
    watch1 = [("国主", "The king waited.")] * 2
    chapters = _chapters([freq0 + title0 + watch0, freq1 + title1 + watch1])

    ranked = detect_mid_frequency_drift(
        chapters,
        source_lang="zh",
        target_lang="en",
        extra_watch_sources={"国主": (), "宰辅": (), "棋局": ()},
    )
    order = [c.source for c in ranked]
    assert order[0] == "宰辅"
    assert set(order) == {"国主", "宰辅", "棋局"}
    top = detect_mid_frequency_drift(
        chapters,
        source_lang="zh",
        target_lang="en",
        max_clusters=1,
        extra_watch_sources={"国主": (), "宰辅": (), "棋局": ()},
    )
    assert [c.source for c in top] == ["宰辅"]


def test_drift_handles_ies_and_ie_plurals_names_and_longer_terms():
    chapters = _chapters(
        [
            [("敌人", "The enemy waited.")] * 3
            + [("女武", "The valkyrie landed.")] * 3
            + [("公主", "The princess arrived.")] * 3
            + [("哈里", "Then Harry nodded.")] * 3
            + [("一阳指", "He struck with Solar Finger.")] * 3
            + [("手里剑", "He threw a shuriken swiftly.")] * 3
            + [("地方官", "The magistrate arrived.")] * 3
            + [("朋友圈", "She checked her feed.")] * 3
            + [("事务所", "The agency closed early.")] * 3,
            [("敌人", "The enemies waited.")] * 3
            + [("女武", "The valkyries landed.")] * 3
            + [("公主", "The princesses arrived.")] * 3
            + [("哈里", "Then Harri nodded.")] * 3
            + [("一阳指", "He struck with Sun Palm.")] * 3
            + [("手里剑", "He threw a dart swiftly.")] * 3
            + [("地方官", "The prefect arrived.")] * 3
            + [("朋友圈", "She checked her timeline.")] * 3
            + [("事务所", "The firm closed early.")] * 3,
        ],
        filler=20,
    )
    watch = {
        "敌人": (),
        "女武": (),
        "公主": (),
        "哈里": (),
        "一阳指": (),
        "手里剑": (),
        "地方官": (),
        "朋友圈": (),
        "事务所": (),
    }
    clusters = detect_mid_frequency_drift(
        chapters,
        source_lang="zh",
        target_lang="en",
        extra_watch_sources=watch,
    )
    sources = [c.source for c in clusters]
    # -y/-ies, -ie/-ies, and sibilant -es plurals collapse without fabricating false drift.
    assert "敌人" not in sources
    assert "女武" not in sources
    assert "公主" not in sources
    # Real splits of names and terms are reported.
    for src in ("哈里", "一阳指", "手里剑", "地方官", "朋友圈", "事务所"):
        assert src in sources
    # max_clusters <= 0 returns empty cleanly.
    assert (
        detect_mid_frequency_drift(
            chapters,
            source_lang="zh",
            target_lang="en",
            max_clusters=0,
            extra_watch_sources=watch,
        )
        == []
    )


def test_inline_gloss_alert_needs_an_explicit_gloss_marker():
    sources = ["他——也就是王老先生——点了点头。", "银河横贯夜空。"]
    drafts = [
        "He (that is, old Mr. Wang) nodded.",
        "The Silver River (lit. 'heavenly river') crossed the night sky.",
    ]
    report = assess_batch_risk(sources, drafts, source_lang="zh", target_lang="en")
    assert _codes(report) == {(1, "inline_gloss")}


def test_japanese_embedded_verse_needs_an_explicit_poetry_word():
    prose = [
        "彼は「New York Times」を読んだ。",
        "仕様書には次のように記されていた。「すべての端末で認証を有効にすること。」",
    ]
    for src in prose:
        assert has_embedded_verse(src, source_lang="ja") is False
    assert has_embedded_verse(
        "彼女は短歌を詠んだ。「春過ぎて夏来にけらし白妙の」", source_lang="ja"
    )


def test_sparse_editor_style_guard_counts_chinese_by_character():
    from wenyi_core.agents.sparse_editor import _is_gratuitous_style_churn

    clauses = [f"他沿着{chr(0x5C71 + i)}路慢慢走" for i in range(22)]
    draft = "，".join(clauses) + "。"
    # A light fluency edit touching most clauses keeps nearly every character.
    candidate = "，".join(c.replace("慢慢", "缓缓") for c in clauses[:16]) + "，"
    candidate += "，".join(clauses[16:]) + "。"
    assert _is_gratuitous_style_churn(draft, candidate, "fluency") is False


def test_sparse_editor_sends_unlineated_verse_alert_once():
    import json

    from wenyi_core.agents.sparse_editor import SparseSemanticEditor
    from wenyi_core.config import Config
    from wenyi_core.llm.configuration import LLMConfig
    from wenyi_core.llm.providers.fake import FakeClient

    prompts: list[str] = []

    def handler(messages, op, json_mode):
        prompts.append("\n".join(m["content"] for m in messages))
        return json.dumps({"patches": []})

    cfg = Config(source_lang="zh", target_lang="en", llm=LLMConfig(preset="fake"))
    editor = SparseSemanticEditor(FakeClient(handler), cfg)
    editor.edit_batch(
        ["白日依山尽，黄河入海流。", "欲穷千里目，更上一层楼。"],
        [
            "The white sun sets behind the hills; the Yellow River flows into the sea.",
            "To see a thousand miles, climb one more storey.",
        ],
    )
    assert prompts
    assert "[Verse formatting alert]" not in prompts[-1]
    assert prompts[-1].count("unlineated_verse") == 1


def test_drift_split_must_involve_a_probe_rendering():
    """A watched title's split is anchored to the Book Bible's own renderings."""
    rows0 = [("Le roi entra au château.", "The king entered the castle.")] * 3
    rows1 = [("Le roi entra au palais.", "The king entered the palace.")] * 3
    chapters = _chapters([rows0, rows1])
    found = detect_mid_frequency_drift(
        chapters,
        source_lang="fr",
        target_lang="en",
        extra_watch_sources={"roi": ("the king", "the monarch")},
    )
    assert found == []


def test_drift_keeps_one_slot_for_overlapping_watched_titles():
    rows0 = [("事务所关门了。", "The agency closed early.")] * 3
    rows1 = [("事务所关门了。", "The firm closed early.")] * 3
    clusters = detect_mid_frequency_drift(
        _chapters([rows0, rows1]),
        source_lang="zh",
        target_lang="en",
        extra_watch_sources={"事务": (), "事务所": ()},
    )
    assert len(clusters) == 1


def _sweep_title_split(tmp_path, *, terms, conflicts=()):
    from unittest.mock import Mock

    from wenyi_core.agents.sparse_editor import SparseEditResult
    from wenyi_core.pipeline.translate.consistency_sweep import ConsistencySweepService
    from wenyi_core.storage.file import FileStorage

    store = FileStorage(str(tmp_path / "state"), create=True)
    rows0 = [("国主下旨。", "Then the sovereign waited.")] * 3
    rows1 = [("国主下旨。", "Then the king waited.")] * 3
    for chapter in _chapters([rows0, rows1]):
        store.save_chapter(chapter)
    store.open_conflicts = lambda: list(conflicts)
    editor = Mock()
    editor.adjudicate_drift.return_value = {"国主": "the sovereign"}
    editor.edit_batch.side_effect = lambda sources, drafts, **_: SparseEditResult(
        targets=tuple(drafts), patched_indices=(), reasons={}
    )
    svc = ConsistencySweepService(editor, source_lang="zh", target_lang="en")
    svc.run(
        store,
        chapter_indices=[0, 1],
        terms=terms,
        divergent_titles={"国主": ("the sovereign", "the king")},
    )
    return editor


def test_consistency_sweep_pins_watched_title_drift_to_its_glossary_target(tmp_path):
    # A generic title the glossary-miss pass skips, so only drift detection sees the split.
    editor = _sweep_title_split(
        tmp_path, terms=[GlossaryTerm(source="国主", target="the king", type="term")]
    )
    # The glossary decides; the adjudicator never overrides it.
    editor.adjudicate_drift.assert_not_called()
    patched_terms = {
        (t.source, t.target)
        for call in editor.edit_batch.call_args_list
        for t in call.kwargs["glossary_terms"]
    }
    assert patched_terms == {("国主", "the king")}


def test_consistency_sweep_leaves_disputed_titles_to_the_user(tmp_path):
    editor = _sweep_title_split(
        tmp_path,
        terms=[GlossaryTerm(source="国主", target="the king", type="term")],
        conflicts=[{"source": "国主", "proposed_target": "the sovereign"}],
    )
    editor.adjudicate_drift.assert_not_called()
    editor.edit_batch.assert_not_called()


def test_consistency_sweep_skips_titles_whose_glossary_entry_is_not_active(tmp_path):
    from wenyi_core.models.glossary import STATUS_CONFLICT

    editor = _sweep_title_split(
        tmp_path,
        terms=[GlossaryTerm(source="国主", target="the king", type="term", status=STATUS_CONFLICT)],
    )
    editor.adjudicate_drift.assert_not_called()
    editor.edit_batch.assert_not_called()


def test_glossary_miss_pass_leaves_disputed_terms_alone(tmp_path):
    from unittest.mock import Mock

    from wenyi_core.pipeline.translate.consistency_sweep import ConsistencySweepService
    from wenyi_core.storage.file import FileStorage

    store = FileStorage(str(tmp_path / "state"), create=True)
    rows0 = [("El obispo rezó.", "The bishop prayed.")] * 2
    rows1 = [("El obispo rezó.", "The high priest prayed.")] * 2
    for chapter in _chapters([rows0, rows1]):
        store.save_chapter(chapter)
    store.open_conflicts = lambda: [{"source": "obispo", "proposed_target": "high priest"}]
    editor = Mock()
    svc = ConsistencySweepService(editor, source_lang="es", target_lang="en")
    svc.run(
        store,
        chapter_indices=[0, 1],
        terms=[GlossaryTerm(source="obispo", target="bishop", type="person")],
    )
    editor.edit_batch.assert_not_called()


def test_consistency_sweep_adjudicates_unglossed_watched_titles(tmp_path):
    editor = _sweep_title_split(tmp_path, terms=[])
    clusters = editor.adjudicate_drift.call_args.args[0]
    assert [c["source"] for c in clusters] == ["国主"]


def test_drift_needs_both_split_words_from_the_probe_renderings():
    scenery0 = [("Don Pedro entra al castillo.", "Don Pedro entered the castle.")] * 3
    scenery1 = [("Don Pedro entra al palacio.", "Don Pedro entered the palace.")] * 3
    assert (
        detect_mid_frequency_drift(
            _chapters([scenery0, scenery1]),
            source_lang="es",
            target_lang="en",
            extra_watch_sources={"Don": ("Don", "Sir")},
        )
        == []
    )
    # Function words in a rendering never anchor ("Her Majesty" does not make "hers" a title).
    majesty0 = [("陛下说那是她的。", "Her Majesty said it was hers.")] * 3
    majesty1 = [("陛下进了宫。", "Her Majesty entered the palace.")] * 3
    assert (
        detect_mid_frequency_drift(
            _chapters([majesty0, majesty1]),
            source_lang="zh",
            target_lang="en",
            extra_watch_sources={"陛下": ("Her Majesty", "Her Highness")},
        )
        == []
    )
    # One probe word paired with scenery from the other chapter is not a split.
    one0 = [("Le roi entra au jardin.", "The king entered the garden.")] * 2 + [
        ("Le roi entra au jardin.", "He entered the garden.")
    ]
    one1 = [("Le roi entra au château.", "He entered the castle.")] * 3
    assert (
        detect_mid_frequency_drift(
            _chapters([one0, one1]),
            source_lang="fr",
            target_lang="en",
            extra_watch_sources={"roi": ("the king", "the monarch")},
        )
        == []
    )
    king0 = [("Le roi aida les gardes.", "The king helped the castle guards.")] * 3
    king1 = [("Le roi aida les soldats.", "The monarch helped the palace soldiers.")] * 3
    clusters = detect_mid_frequency_drift(
        _chapters([king0, king1]),
        source_lang="fr",
        target_lang="en",
        extra_watch_sources={"roi": ("the king", "the monarch")},
    )
    assert [{w for w, _ in c.competing_targets} for c in clusters] == [{"king", "monarch"}]


def test_verse_alert_keeps_its_slot_in_a_full_alert_budget():
    sources = ["白日依山尽，黄河入海流。", "欲穷千里目，更上一层楼。"] + [
        f"他第{i}次走了。" for i in range(6)
    ]
    drafts = [
        "The white sun sets behind the hills; the Yellow River flows into the sea.",
        "To see a thousand miles, climb one more storey.",
    ] + [f"He left 走 for time {i}." for i in range(6)]
    report = assess_batch_risk(sources, drafts, source_lang="zh", target_lang="en")
    assert sum(a.code == "script_residue" for a in report.alerts) == 6
    assert "unlineated_verse" in report.format_prompt_alerts(max_items=6)
    assert report.format_prompt_alerts(max_items=0) == ""


def test_style_guard_rejects_large_chinese_deletions():
    from wenyi_core.agents.sparse_editor import _is_gratuitous_style_churn

    clauses = [f"他沿着{chr(0x5C71 + i)}路慢慢走" for i in range(22)]
    draft = "，".join(clauses) + "。"
    assert _is_gratuitous_style_churn(draft, "，".join(clauses[:4]) + "。", "fluency") is True


def test_inline_gloss_markers_come_from_the_target_language():
    def flagged(draft: str, target_lang: str) -> bool:
        report = assess_batch_risk(["他点头。"], [draft], source_lang="zh", target_lang=target_lang)
        return any(a.code == "inline_gloss" for a in report.alerts)

    assert not flagged("It was a good (meaningful) choice.", "en")
    assert not flagged("See the field (meaning_test) below.", "en")
    assert flagged("He bowed (translator’s note: a court gesture).", "en")
    assert flagged("Il hocha la tête (litt. « oui »).", "fr")
    assert flagged("他点头（直译为“是”）。", "zh")
    # A language without markers in its profile gets no inline-gloss alert.
    assert not flagged("그는 고개를 끄덕였다 (lit. yes).", "ko")
