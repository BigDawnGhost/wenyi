"""Full-book scale regressions for the structural batch pre-screener and consistency drift miner.

Covers glossary miss matching (case-insensitivity, conservative English plurals, CJK numeral
boundary isolation, covering-term deduplication, and batch-level pre-filtering), embedded verse
precision against prose dialogue verbs, concise-reasoning sparse-editor prompts, and everyday
Chinese vocabulary / English inflection filtering in ``detect_mid_frequency_drift``.
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
        "十一回头望去。",
        "十一回到屋内。",
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
        "Shi Yi returned inside.",
    ]
    report = assess_batch_risk(
        sources, drafts, source_lang="zh", target_lang="en", glossary_terms=terms
    )
    assert _codes(report) == {(6, "glossary_miss"), (7, "glossary_miss"), (8, "glossary_miss")}


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
            chapters, source_lang="zh", target_lang="en", existing_glossary_sources=set()
        )
    }
    assert "征召" not in sources
    assert "幕僚" in sources


def test_drift_rejects_everyday_vocabulary_and_fragments():
    everyday = [
        ("需要", ("need", "require")),
        ("机会", ("chance", "opportunity")),
        ("离开", ("depart", "quit")),
        ("整个", ("whole", "entire")),
        ("一口", ("mouthful", "gulp")),
        ("间里", ("chamber", "hall")),
    ]
    ch0: list[tuple[str, str]] = []
    ch1: list[tuple[str, str]] = []
    for src, (a, b) in everyday:
        ch0 += [(src, f"Some {a} here."), (src, f"Another {a} there.")]
        ch1 += [(src, f"Some {b} here."), (src, f"Another {b} there.")]
    sources = {
        c.source
        for c in detect_mid_frequency_drift(
            _chapters([ch0, ch1]),
            source_lang="zh",
            target_lang="en",
            existing_glossary_sources=set(),
        )
    }
    assert not sources & {src for src, _ in everyday}


def _split_rows(
    src: str, a: str, b: str, n: int
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    half = n // 2
    return (
        [(src, f"Then the {a} waited.") for _ in range(half)],
        [(src, f"Then the {b} waited.") for _ in range(n - half)],
    )


def test_drift_caps_document_frequency_monotonically():
    hot0, hot1 = _split_rows("棋局", "board", "match", 210)
    # 210 hits exceeds max(200, 1% of paragraphs): everyday at book scale, so rejected.
    big = _chapters([hot0, hot1], filler=2500)
    assert not any(
        c.source == "棋局"
        for c in detect_mid_frequency_drift(
            big, source_lang="zh", target_lang="en", existing_glossary_sources=set()
        )
    )
    # Watched titles bypass the cap.
    assert any(
        c.source == "棋局"
        for c in detect_mid_frequency_drift(
            big,
            source_lang="zh",
            target_lang="en",
            existing_glossary_sources=set(),
            extra_watch_sources={"棋局": ("the board",)},
        )
    )
    # Terms below the 200-paragraph floor are kept on smaller books.
    small0, small1 = _split_rows("棋局", "board", "match", 20)
    small = _chapters([small0, small1], filler=110)
    assert any(
        c.source == "棋局"
        for c in detect_mid_frequency_drift(
            small, source_lang="zh", target_lang="en", existing_glossary_sources=set()
        )
    )


def test_drift_ranking_prefers_watched_then_title_signal_over_raw_frequency():
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
        existing_glossary_sources=set(),
        extra_watch_sources={"国主": ("the sovereign", "the king")},
    )
    order = [c.source for c in ranked]
    assert order[:3] == ["国主", "宰辅", "棋局"]
    top = detect_mid_frequency_drift(
        chapters,
        source_lang="zh",
        target_lang="en",
        existing_glossary_sources=set(),
        max_clusters=1,
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
            + [("事务所", "The agency closed early.")] * 3
            + [("特权阶级", "The privileged elite ruled.")] * 3,
            [("敌人", "The enemies waited.")] * 3
            + [("女武", "The valkyries landed.")] * 3
            + [("公主", "The princesses arrived.")] * 3
            + [("哈里", "Then Harri nodded.")] * 3
            + [("一阳指", "He struck with Sun Palm.")] * 3
            + [("手里剑", "He threw a dart swiftly.")] * 3
            + [("地方官", "The prefect arrived.")] * 3
            + [("朋友圈", "She checked her timeline.")] * 3
            + [("事务所", "The firm closed early.")] * 3
            + [("特权阶级", "The privileged caste ruled.")] * 3,
        ],
        filler=20,
    )
    clusters = detect_mid_frequency_drift(
        chapters, source_lang="zh", target_lang="en", existing_glossary_sources=set()
    )
    sources = [c.source for c in clusters]
    # -y/-ies, -ie/-ies, and sibilant -es plurals collapse without fabricating false drift.
    assert "敌人" not in sources
    assert "女武" not in sources
    assert "公主" not in sources
    # Transliterated names, terms starting with 一, and 3-char compounds with everyday bigrams survive.
    assert "哈里" in sources
    assert "一阳指" in sources
    assert "手里剑" in sources
    assert "地方官" in sources
    assert "朋友圈" in sources
    # Complete 3-char term beats its 2-char prefix on equal split quality/hits.
    assert "事务所" in sources
    assert "事务" not in sources
    # Overlapping 2/3-char fragments of 特权阶级 sharing (elite, caste) collapse to one slot.
    elite_caste = [
        c for c in clusters if {w.casefold() for w, _ in c.competing_targets} == {"elite", "caste"}
    ]
    assert len(elite_caste) == 1
    # max_clusters <= 0 returns empty cleanly.
    assert (
        detect_mid_frequency_drift(
            chapters,
            source_lang="zh",
            target_lang="en",
            existing_glossary_sources=set(),
            max_clusters=0,
        )
        == []
    )
