"""Full-book scale regressions for the batch risk pre-screener and consistency drift miner.

Covers false positives seen on a 1,479-chapter run: ordinary English words colliding with
glossary names in ``phantom_entity`` checks, everyday Chinese vocabulary / English
inflections crowding real term drift out of ``detect_mid_frequency_drift``, mid-clause and
temporal ``不…，就`` patterns firing ``conditional_logic``, and the batch-level term
pre-filtering that keeps first/middle/last-paragraph hits cheap without losing alerts.
"""

from __future__ import annotations

from wenyi_core.agents.batch_risk import (
    _ZH_CONDITIONAL_RE,
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


# --- batch_risk: zh conditional precision -------------------------------------------------


def test_zh_conditional_regex_true_positives():
    # Sentence/quote/ellipsis/paren/line starts and explicit conditional introducers
    # (including with a short subject or normally-excluded 不-compound) are real correlatives.
    for text in (
        "不守北关以免后患，就先取西原以稳大局。",
        "不答应，就别想走。",
        "“不尊重我的，就滚出去。”",
        "……不交钱，就别走。",
        "（不交钱，就别走）",
        "(不交钱，就别走。)",
        "‘不交钱，就别走’",
        "如果不臣服，就毁灭。",
        "如果不是你，就完了。",
        "如果不知道答案，就问老师。",
        "如果他不来，就走。",
        "要是不同意，就再议。",
        "要是大家不同意，就再议。",
        "要是不敢反抗，就只能挨打。",
        "倘若不低头，就被打压。",
        "倘若不到万不得已，就绝不动手。",
        "假如不去赴宴，就算弃权。",
        "只要不下雨，就出发。",
        "一旦不注意，就会出事。",
        "若不低头，就退无可退。",
        "他若不至，便开战。",
        "他沉默良久。不投降，就屠城。",
        "前路已断。\n不低头，就退无可退。",
    ):
        assert _ZH_CONDITIONAL_RE.search(text), text


def test_zh_conditional_regex_rejects_mid_clause_and_temporal_false_positives():
    for text in (
        # Mid-clause 不: the correlative reading does not exist.
        "他不需要休息，就去睡觉了。",
        "城墙挡不住敌人的进攻，就被攻破了。",
        "他站在门口，不需要帮忙，就自己搬进去了。",
        # Temporal/aspectual openers at sentence start are not conditionals.
        "不到十分钟，就到了城下。",
        "不一会儿，就到了城下。",
        "不多时，就到了。",
        "不多久，就来了。",
        "不一时，就来了。",
        "不大一会儿，就到了。",
        "不消片刻，就到了。",
        "不出三日，就到了。",
        "不经意间，就到了。",
        "不得已，就走了。",
        "不等总统回应，就宣布了结果。",
        "不敢怠慢，就连夜出发了。",
        "不知不觉间，就到了天明。",
        # Non-conditional clause-initial 不-compounds.
        "不过是一场误会，就别计较了。",
        "不管对方说什么，就当没听见。",
        "不是不想去，就是没时间。",
        "不必担心，就有我在。",
        # The span must not run past closing quotes/parens or clause/line boundaries.
        "所谓“不犯法”的方式，就是钻空子。",
        "所谓‘不犯法’的方式，就是钻空子。",
        "（不交钱）的情况下，就别走了。",
        "(不交钱)的情况下，就别走了。",
        "（不交钱)的情况下，就别走了。",
        "不吃,不喝,就躺着。",
        "不日即可抵达；次日，就出发了。",
        "不辞而别：次日，就有人来报。",
        "不日即可抵达\n次日，就出发了。",
    ):
        assert not _ZH_CONDITIONAL_RE.search(text), text


def test_zh_conditional_regex_keeps_ru_branches():
    for text in (
        "若是将军亲自领兵，便能稳守北原。",
        "若非亲眼所见，绝不敢信。",
        "如若不然，定当严惩。",
    ):
        assert _ZH_CONDITIONAL_RE.search(text), text


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
    assert report.should_escalate is True


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


def test_batch_prefilter_catches_phantom_entity_on_first_middle_last_paragraphs():
    terms = [GlossaryTerm(source="临江", target="Riverside", type="place")]
    sources = [
        "船只顺流而下。",
        "两岸青山如画。",
        "暮色四合。",
        "渔火点点。",
        "夜色渐深。",
    ]
    drafts = [
        "They moored at Riverside by dusk.",
        "Green hills slid past on both banks.",
        "Riverside lights flickered on the water.",
        "Fishing fires glimmered far off.",
        "The boat slipped past Riverside in the dark.",
    ]
    report = assess_batch_risk(
        sources, drafts, source_lang="zh", target_lang="en", glossary_terms=terms
    )
    codes = _codes(report)
    for idx in (0, 2, 4):
        assert (idx, "phantom_entity") in codes
    assert not any(idx in {1, 3} for idx, code in codes if code == "phantom_entity")
    assert report.should_escalate is True


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
    # so neither paragraph may raise a glossary_miss or phantom_entity alert.
    assert not any(a.code in {"glossary_miss", "phantom_entity"} for a in report.alerts)


def test_phantom_entity_target_overlap_checked_before_source_stem():
    terms = [
        GlossaryTerm(source="雪峰城", target="Snowpeak City", type="place"),
        GlossaryTerm(source="雪峰", target="Snowpeak", type="place"),
        GlossaryTerm(source="临江", target="Riverside", type="place"),
    ]
    sources = [
        "商队缓缓驶向雪峰。",
        "他继续赶路。",
    ]
    drafts = [
        "The caravan rolled toward Snowpeak City.",
        "He passed Riverside on the way.",
    ]
    report = assess_batch_risk(
        sources, drafts, source_lang="zh", target_lang="en", glossary_terms=terms
    )
    codes = _codes(report)
    # "Snowpeak City" hits the draft, but the overlapping "Snowpeak" term's source 雪峰
    # really appears in this paragraph, so the overlap dedup suppresses the alert.
    assert (0, "phantom_entity") not in codes
    # A genuinely unexplained target still fires on the other paragraph.
    assert (1, "phantom_entity") in codes
    assert report.should_escalate is True


def test_phantom_multiline_target_survives_batch_prefilter():
    terms = [GlossaryTerm(source="东吴", target="Eastern Wu", type="place")]
    sources = ["北原大战结束后，诸将班师。"]
    drafts = ["Envoys from Eastern\nWu arrived at dusk."]
    report = assess_batch_risk(
        sources, drafts, source_lang="zh", target_lang="en", glossary_terms=terms
    )
    # A multi-word hit spanning a line break must survive the whitespace-collapsed
    # batch pre-hit text and still raise the phantom_entity alert.
    assert any(a.code == "phantom_entity" for a in report.alerts)


def test_assess_batch_risk_keeps_hits_at_full_book_scale():
    terms = [
        GlossaryTerm(source=f"虚指{i:03d}", target=f"Nowhere{i:03d}", type="place")
        for i in range(240)
    ]
    terms[10] = GlossaryTerm(source="黑石城", target="Blackstone City", type="place")
    terms[11] = GlossaryTerm(source="临江", target="Riverside", type="place")
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
    sources[50] = "船只顺流而下，两岸青山如画。"
    drafts[50] = "The boat drifted past Riverside on the way downstream."
    sources[150] = "暮色四合，渔火点点。"
    drafts[150] = "Dusk fell; Riverside lights flickered on the water."
    report = assess_batch_risk(
        sources, drafts, source_lang="zh", target_lang="en", glossary_terms=terms
    )
    codes = _codes(report)
    # 242 terms against 200 paragraphs: the batch pre-filter must keep every real hit on
    # the first, middle and last paragraphs while skipping the absent filler terms.
    for idx in (0, 100, 199):
        assert (idx, "glossary_miss") in codes
    for idx in (50, 150):
        assert (idx, "phantom_entity") in codes
    assert report.should_escalate is True


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
