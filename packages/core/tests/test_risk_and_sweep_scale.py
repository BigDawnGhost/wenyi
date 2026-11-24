"""Full-book scale regressions for the batch risk pre-screener and consistency drift miner.

Covers false positives seen on a 1,479-chapter run: ordinary English words colliding with
glossary names in ``phantom_entity`` checks, and everyday Chinese vocabulary / English
inflections crowding real term drift out of ``detect_mid_frequency_drift``.
"""

from __future__ import annotations

from wenyi_core.agents.batch_risk import (
    _lowercase_words,
    _phantom_target_hit,
    _target_contains_term,
    assess_batch_risk,
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


# --- batch_risk: phantom_entity ---------------------------------------------------------


def test_phantom_entity_ignores_lowercase_and_sentence_initial_common_words():
    sources = [
        "他说他明天会去城里。",
        "她心里还存着一丝希望。",
        "白烟从屋顶升起。",
        "你会去吗？",
        "希望渐渐破灭了。",
        "白烟升起。",
        "他问道：“你会留下吗？”",
    ]
    drafts = [
        "He said he will go into the city tomorrow.",
        "She still held on to a thread of hope.",
        "A white smoke curled up from the roof.",
        "Will you go?",
        "Hope faded little by little.",
        "White smoke rose.",
        'He asked, "Will you stay?"',
    ]
    report = assess_batch_risk(
        sources, drafts, source_lang="zh", target_lang="en", glossary_terms=_NAME_TERMS
    )
    assert not any(a.code == "phantom_entity" for a in report.alerts)
    assert report.should_escalate is False


def test_phantom_entity_still_catches_mid_sentence_names_and_distinctive_sentence_start():
    sources = ["他转身看着来人。", "他骑马回到了城里。", "城池陷落了。", "她叹了口气。"]
    drafts = [
        "He turned and looked at Will.",
        "He rode back to Jingxiang.",
        "Jingxiang fell.",
        "She sighed, and White said nothing.",
    ]
    report = assess_batch_risk(
        sources, drafts, source_lang="zh", target_lang="en", glossary_terms=_NAME_TERMS
    )
    codes = _codes(report)
    for idx in range(4):
        assert (idx, "phantom_entity") in codes
    assert report.should_escalate is True


def test_phantom_entity_skips_when_source_names_the_entity():
    report = assess_batch_risk(
        ["威尔推门而入。"],
        ["Will pushed the door open."],
        source_lang="zh",
        target_lang="en",
        glossary_terms=_NAME_TERMS,
    )
    assert not report.alerts


def test_phantom_target_hit_rules():
    # Case-sensitive on Latin targets.
    assert not _phantom_target_hit("I will go.", "Will", target_lang="en")
    assert _phantom_target_hit("I saw Will go.", "Will", target_lang="en")
    # Sentence/line/quote/dash/ellipsis starts are ambiguous only for common words.
    for text in (
        "Will you go?",
        "Done. Will you go?",
        '"Will you go?"',
        "Yes.\nWill you?",
        "—Will you go?",
        "…Will came.",
        "nodded.—Will stood.",
    ):
        assert not _phantom_target_hit(text, "Will", target_lang="en")
    assert _phantom_target_hit("Done. Jingxiang fell.", "Jingxiang", target_lang="en")
    assert _phantom_target_hit('"Jingxiang fell," he said.', "Jingxiang", target_lang="en")
    # A word written lowercase elsewhere in the batch is ambiguous at sentence start too,
    # including NFD-decomposed Latin characters.
    assert _phantom_target_hit("Rowan branches swayed.", "Rowan", target_lang="en")
    assert not _phantom_target_hit(
        "Rowan branches swayed.",
        "Rowan",
        target_lang="en",
        lowercase_words=frozenset({"rowan"}),
    )
    assert "café" in _lowercase_words(["a cafe\u0301 on the corner"])
    # Multi-word targets (including across line breaks), honorific prefixes, apostrophe variants,
    # lowercase particles, and articles.
    assert _phantom_target_hit("Envoys from the Eastern Wu court.", "Eastern Wu", target_lang="en")
    assert _phantom_target_hit("Envoys from Eastern\nWu arrived.", "Eastern Wu", target_lang="en")
    assert _phantom_target_hit("Envoys from Eastern\r\nWu arrived.", "Eastern Wu", target_lang="en")
    assert not _phantom_target_hit(
        "Envoys from Eastern\nwu arrived.", "Eastern Wu", target_lang="en"
    )
    assert _phantom_target_hit("The Duke of North smiled.", "the Duke of North", target_lang="en")
    assert not _phantom_target_hit("the eastern wu court", "Eastern Wu", target_lang="en")
    assert _phantom_target_hit("He met Mr. White at noon.", "White", target_lang="en")
    assert _phantom_target_hit("Then Dr. Hope came in.", "Hope", target_lang="en")
    assert _phantom_target_hit("He called O\u2019Brien over.", "O'Brien", target_lang="en")
    assert _phantom_target_hit(
        "De Gaulle arrived.", "de Gaulle", target_lang="en", term_type="person"
    )
    # Person names never strip 'An'/'A' as an indefinite article ("An Lushan" != "Lushan").
    assert not _phantom_target_hit(
        "They climbed Lushan.", "An Lushan", target_lang="en", term_type="person"
    )
    # English collision list is gated to English; other Latin languages rely on lowercase_words.
    assert _phantom_target_hit("Will arriva tard.", "Will", target_lang="fr")
    # CJK targets keep the existing substring matching.
    assert _phantom_target_hit("他望向京襄城。", "京襄", target_lang="zh")


def test_glossary_miss_matching_stays_case_insensitive():
    # Canonical-miss detection must not be weakened by the stricter phantom matcher.
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
