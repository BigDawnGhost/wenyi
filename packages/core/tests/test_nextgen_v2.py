"""Unit tests for v2 quality hill-climbing improvements:
- Provider-safe thinking escalation (ThinkingControl.escalate, escalate_route_thinking, RoutedLLMClient)
- Deterministic draft anomaly pre-screener (batch_risk.py) & risk-routed SparseSemanticEditor
- Multilingual Book Bible mining (Japanese katakana/honorifics/ruby, Latin connectors/corpus),
  index-key probe fallback, morpheme-family clustering, spoiler-safe gender guard, conventions
- Wave-local & cross-chapter pre-rendered QuoteMemory
- Post-normalize fixes (straight-quote preservation, intra-paragraph mixed spelling, CJK hygiene/names)
- Cross-language pair sheets (ja->en, en->zh) & base-locale fallback (zh->en-US/en-GB)
- Post-parallel ConsistencySweepService
"""

from __future__ import annotations

import json

from wenyi_core.agents.batch_risk import assess_batch_risk, has_embedded_verse
from wenyi_core.agents.book_bible import (
    BibleCandidate,
    BookBibleAgent,
    _apply_spoiler_safe_gender_guard,
    _cluster_candidate_rows,
    mine_bible_candidates,
)
from wenyi_core.agents.sparse_editor import SparseSemanticEditor
from wenyi_core.config import Config
from wenyi_core.i18n.languages import honorific_rule, resolve_pair_key
from wenyi_core.ingest.segmenter import is_verse_segment
from wenyi_core.llm.configuration import LLMConfig
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.llm.routing import escalate_route_thinking, operation_route
from wenyi_core.llm.thinking import effort_control
from wenyi_core.models.document import Chapter, Segment
from wenyi_core.models.glossary import GlossaryTerm
from wenyi_core.pipeline.translate.consistency_sweep import (
    ConsistencySweepService,
    detect_mid_frequency_drift,
)
from wenyi_core.pipeline.translate.post_normalize import (
    normalize_chapter_cjk_typography,
    normalize_chapter_latin_typography,
    normalize_minority_names,
)
from wenyi_core.pipeline.translate.quote_memory import QuoteMemory


def test_thinking_control_escalates_monotonically_and_respects_off_and_ceiling():
    openai_ctrl = effort_control("off", "minimal", "low", "medium", "high", "xhigh")
    assert openai_ctrl.escalate({"thinking": True, "reasoning_effort": "low"}) == "medium"
    assert openai_ctrl.escalate({"thinking": True, "reasoning_effort": "medium"}) == "high"
    # Never auto-escalates past default ceiling "high" into ultra-expensive "xhigh".
    assert openai_ctrl.escalate({"thinking": True, "reasoning_effort": "high"}) is None
    # Never silently turns on thinking when user explicitly configured off.
    assert openai_ctrl.escalate({"thinking": False, "reasoning_effort": "low"}) is None
    # Respects raw extra_body overrides.
    assert (
        openai_ctrl.escalate(
            {"thinking": True, "reasoning_effort": "low", "extra_body": {"reasoning": "none"}}
        )
        is None
    )

    # DeepSeek ladder has no "medium" -> low cleanly steps up to "high".
    deepseek_ctrl = effort_control("off", "low", "high", "max")
    assert deepseek_ctrl.escalate({"thinking": True, "reasoning_effort": "low"}) == "high"


def test_escalate_route_thinking_steps_up_route_and_updates_fingerprint():
    cfg = LLMConfig.model_validate(
        {
            "providers": {
                "magpie": {
                    "kind": "openai-compatible",
                    "base_url": "http://127.0.0.1:3425/v1",
                    "reasoning_style": "openai",
                }
            },
            "models": {
                "magpie/test-model": {"options": {"thinking": True, "reasoning_effort": "high"}}
            },
            "routes": {
                op: {"model": "magpie/test-model", "thinking": "low"}
                for op in LLMConfig.model_validate({"preset": "fake"}).routes
            },
        }
    )
    base = operation_route(cfg, "polish.body")
    boosted = escalate_route_thinking(cfg, base, steps=1)
    assert boosted.fingerprint != base.fingerprint
    assert json.loads(boosted.options_json)["reasoning_effort"] == "medium"


def test_pair_sheets_resolve_ja_en_en_zh_and_regional_fallbacks():
    assert resolve_pair_key("zh", "en-US") == "zh__en"
    assert resolve_pair_key("zh-Hant", "en-GB") == "zh-Hant__en"
    assert resolve_pair_key("ja", "en") == "ja__en"
    assert resolve_pair_key("en", "zh") == "en__zh"
    assert "katakana" in honorific_rule("ja", "en").lower()
    assert "hypotactic" in honorific_rule("en", "zh").lower()


def test_book_bible_probe_accepts_numeric_index_keys():
    cfg = Config(source_lang="zh", target_lang="en", llm=LLMConfig(preset="fake"))

    def handler(messages, operation, json_mode):
        user = messages[-1]["content"]
        if "Render each term" in user:
            # Simulate DeepSeek returning numeric index keys "0", "1", ...
            return json.dumps({"renderings": {"0": "Southern Chu", "1": "Jiang Zhe"}})
        return json.dumps(
            {
                "terms": [
                    {"source": "南楚", "target": "Southern Chu", "type": "place"},
                    {"source": "江哲", "target": "Jiang Zhe", "type": "person", "gender": "male"},
                ],
                "conventions": ["Render 国主 consistently as 'the sovereign'."],
            }
        )

    agent = BookBibleAgent(FakeClient(handler), cfg)
    res = agent.compile_bible(
        ["南楚 and 江哲 debate at court."],
        "南楚大军压境。南楚君臣震动。江哲微笑。江哲拱手。",
    )
    assert {t.source: t.target for t in res.terms} == {"南楚": "Southern Chu", "江哲": "Jiang Zhe"}
    assert res.conventions == ("Render 国主 consistently as 'the sovereign'.",)


def test_book_bible_mines_japanese_katakana_honorifics_and_ruby():
    digests = ["黒崎悠真 and セレスティア visit ヴァルハイム."]
    corpus = (
        "黒〘くろ〙崎〘さき〙悠〘ゆう〙真〘ま〙はセレスティア様とヴァルハイムへ行った。"
        "美咲殿の手紙にもヴァルハイムとセレスティアと黒崎悠真の名があった。"
        "美咲殿は《星見の石板》を探していた。《星見の石板》は古い。"
    )
    cands = {c.source for c in mine_bible_candidates(digests, corpus, source_lang="ja")}
    assert {"セレスティア", "ヴァルハイム", "美咲", "星見の石板", "黒崎悠真"} <= cands


def test_book_bible_mines_latin_connectors_and_corpus_entities():
    digests = ["Edmund Burke debates the Revolution Society."]
    corpus = (
        "Edmund Burke addressed the House of Commons in London. "
        "The House of Commons rejected the Revolution Society and praised Edmund Burke. "
        "Meanwhile, Dr. Price spoke of the Rights of Man. Dr. Price repeated the Rights of Man."
    )
    cands = {c.source for c in mine_bible_candidates(digests, corpus, source_lang="en")}
    assert "House of Commons" in cands
    assert "Edmund Burke" in cands
    assert "Dr. Price" in cands
    assert "Meanwhile" not in cands


def test_book_bible_morpheme_clustering_and_spoiler_safe_gender_guard():
    cands = [
        (BibleCandidate("明月楼", 4, True, "", ""), "Misty Moon", "Bright Moon Tower", True),
        (
            BibleCandidate("明月公主", 3, True, "", ""),
            "Princess Mingyue",
            "Princess Mingyue",
            False,
        ),
        (BibleCandidate("江哲", 9, True, "", ""), "Jiang Zhe", "Jiang Zhe", False),
    ]
    rows = _cluster_candidate_rows(cands)
    assert any("[Linked family: 明月]" in r for r in rows)

    terms = [
        GlossaryTerm(source="秦公子", target="Young Master Qin", type="person", gender="female"),
        GlossaryTerm(source="梁婉", target="Liang Wan", type="person", gender="female"),
    ]
    corpus = "秦公子微微一笑，他端起茶杯。后来秦公子敛衽一礼，她低声道。梁婉笑道，她很得意。"
    guarded = _apply_spoiler_safe_gender_guard(terms, corpus, source_lang="zh")
    by_src = {t.source: t for t in guarded}
    assert by_src["秦公子"].gender == ""
    assert "do not spoil" in by_src["秦公子"].note.lower()
    assert by_src["梁婉"].gender == "female"


def test_batch_risk_screener_catches_traps_and_escalates_sparse_editor():
    terms = [GlossaryTerm(source="明月楼", target="Mingyue Tower", type="place")]
    sources = [
        "不攻蜀以免心腹之患，就平南楚以求清卧榻之侧。",
        "江哲来到明月楼前，吟道：“八百里分麾下炙，五十弦翻塞外声。”",
        "有尚大人在，孤可以旦夕宴饮了。",
    ]
    drafts = [
        "Whether to leave Shu unattacked to spare ourselves a threat.",
        "Jiang Zhe arrived at Misty Moon Pavilion and recited, 'Eight hundred li of roast meat.'",
        "With Chancellor Shang here, the lone one could feast morning and night.",
    ]
    report = assess_batch_risk(
        sources,
        drafts,
        source_lang="zh",
        target_lang="en",
        glossary_terms=terms,
    )
    codes = {a.code for a in report.alerts}
    assert report.should_escalate is True
    assert {"conditional_logic", "glossary_miss", "allusion", "royal_pronoun"} <= codes
    assert has_embedded_verse(sources[1], source_lang="zh") is True

    # Verify SparseSemanticEditor passes thinking_boost=1 on risky batch and 0 on clean batch.
    cfg = Config(source_lang="zh", target_lang="en", llm=LLMConfig(preset="fake"))
    fake = FakeClient(lambda m, op, jm: json.dumps({"patches": []}))
    editor = SparseSemanticEditor(fake, cfg)
    res_risky = editor.edit_batch(sources, drafts, glossary_terms=terms)
    assert res_risky.thinking_boost == 1
    assert fake.calls[-1]["thinking_boost"] == 1

    res_clean = editor.edit_batch(["天气很好。"], ["The weather was fine."], glossary_terms=terms)
    assert res_clean.thinking_boost == 0
    assert fake.calls[-1]["thinking_boost"] == 0


def test_sparse_editor_voice_guard_rejects_gratuitous_style_rewrites():
    cfg = Config(source_lang="zh", target_lang="en", llm=LLMConfig(preset="fake"))
    draft = (
        "My coffin is not yet closed, so the verdict is hardly in, yet there can be no harm "
        "in reading the chronicle a little early while the tea is still warm."
    )
    flattened = (
        "Because he had not passed away yet, final judgment remained pending, though examining "
        "historical records beforehand caused no injury during afternoon refreshment."
    )
    fake = FakeClient(
        lambda m, op, jm: json.dumps(
            {"patches": [{"index": 0, "reason": "style", "text": flattened}]}
        )
    )
    editor = SparseSemanticEditor(fake, cfg)
    res = editor.edit_batch(["盖棺论定，尚早得很。"], [draft])
    assert res.targets[0] == draft
    assert res.patched_indices == ()


def test_post_normalize_fixes_straight_quotes_intra_para_and_cjk():
    # Straight-quoted chapter must NOT receive curly quotes on single-quote dialogue repair.
    pairs = [
        ("他说。", 'He said, "Welcome home."'),
        ("她答。", "'I have been waiting for hours.'"),
    ]
    cleaned, _ = normalize_chapter_latin_typography(pairs, target_lang="en")
    assert cleaned[1] == '"I have been waiting for hours."'
    assert "“" not in cleaned[1]

    # Mid-sentence spaced em dashes normalize to tight em dashes while attribution dashes stay intact.
    em_cleaned, _ = normalize_chapter_latin_typography(
        [("他笑道。", "I knew it — you agree.\n—History of Southern Chu")],
        target_lang="en",
    )
    assert em_cleaned[0] == "I knew it—you agree.\n—History of Southern Chu"

    # Intra-paragraph mixed spelling (both "Sanzang" and "San Zang" in same paragraph) is fixed.
    terms = [GlossaryTerm(source="三藏", target="Sanzang", type="person")]
    out, subs = normalize_minority_names(
        [("三藏说道，三藏大喜。", "Sanzang spoke, and San Zang rejoiced.")],
        terms,
        target_lang="en",
    )
    assert out[0] == "Sanzang spoke, and Sanzang rejoiced."
    assert len(subs) == 1

    # Shorter variant ("Gao") must never match inside existing canonical ("Old Gao" -> never "Old Old Gao").
    gao_terms = [GlossaryTerm(source="高老", target="Old Gao", type="person")]
    gao_out, gao_subs = normalize_minority_names(
        [("高老笑道，高老点头。", "Old Gao smiled, and Gao nodded.")],
        gao_terms,
        variants_by_source={"高老": ("Gao",)},
        target_lang="en",
    )
    assert gao_out[0] == "Old Gao smiled, and Old Gao nodded."
    assert len(gao_subs) == 1

    # CJK target minority-name normalization & CJK punctuation hygiene work without uppercase letters.
    zh_terms = [GlossaryTerm(source="Edmund Burke", target="埃德蒙·伯克", type="person")]
    zh_out, zh_subs = normalize_minority_names(
        [("Edmund Burke wrote.", "爱德蒙·伯克写道。")],
        zh_terms,
        variants_by_source={"Edmund Burke": ("爱德蒙·伯克",)},
        target_lang="zh",
    )
    assert zh_out[0] == "埃德蒙·伯克写道。"
    assert len(zh_subs) == 1

    cjk_cleaned, cjk_fixes = normalize_chapter_cjk_typography(
        [("Test", "他说这真是--奇事！保留 `a,b--c` 与 $x,y$。")]
    )
    assert cjk_cleaned[0] == "他说这真是——奇事！保留 `a,b--c` 与 $x,y$。"
    assert cjk_fixes == 1


def test_quote_memory_clone_and_cross_chapter_preseed():
    chapters = [
        (0, ["「星は沈まず、ただ人の目が曇るのみ。」"]),
        (1, ["彼女は「星は沈まず、ただ人の目が曇るのみ。」と書いた。"]),
    ]
    qm = QuoteMemory.from_chapters(chapters)
    spans = qm.cross_chapter_spans()
    assert len(spans) == 1 and "星は沈まず" in spans[0]
    qm.seed_canonical(spans[0], "Stars never sink; only mortal eyes grow dim.")
    clone = qm.clone()
    refs = clone.references_for_batch(chapters[1][1])
    assert len(refs) == 1 and refs[0][1] == "Stars never sink; only mortal eyes grow dim."
    # Partial sub-sentence callback inside a longer quoted poem links to the later re-quote.
    sub_qm = QuoteMemory.from_chapters(
        [
            (
                0,
                [
                    "“春日游，杏花吹满头。妾拟将身嫁与，纵被无情弃，不能羞。”",
                    "读到“妾拟将身嫁与，纵被无情弃，不能羞。”时，泪水终于流了下来。",
                ],
            ),
        ]
    )
    assert sub_qm.repeated_count >= 1
    sub_qm.observe(
        (0, 0),
        "“春日游，杏花吹满头。妾拟将身嫁与，纵被无情弃，不能羞。”",
        '"A spring outing.\nI would give myself to him; cast aside unfeeling, I feel no shame."',
    )
    assert sub_qm.references_for_batch(
        ["读到“妾拟将身嫁与，纵被无情弃，不能羞。”时，泪水终于流了下来。"]
    )


def test_is_verse_segment_avoids_asymmetric_prose_false_positive():
    assert is_verse_segment("花果山福地，水簾洞洞天。") is True
    assert is_verse_segment("他站起身，仔细打量着眼前的书生。") is False


def test_consistency_sweep_unifies_quotes_and_detects_mid_freq_drift(tmp_path):
    from wenyi_core.storage.file import FileStorage

    store = FileStorage(str(tmp_path), create=True)
    ch0 = Chapter(
        index=0,
        title="Ch 0",
        segments=[
            Segment(
                index=0,
                source="妾拟将身嫁与，纵被无情弃，不能羞。",
                target="I mean to give myself to you; cast aside, I feel no shame.",
            ),
            Segment(index=1, source="幕僚纷纷议论。", target="The advisers murmured in debate."),
            Segment(index=2, source="幕僚点头称是。", target="The adviser nodded in agreement."),
            Segment(index=3, source="幕僚退下。", target="The adviser withdrew quietly."),
            Segment(index=4, source="众幕僚沉默。", target="All the advisers fell silent."),
            Segment(
                index=5, source="普通叙述。", target="Ordinary narration continues smoothly here."
            ),
        ],
    )
    ch1 = Chapter(
        index=1,
        title="Ch 1",
        segments=[
            Segment(
                index=0,
                source="妾拟将身嫁与，纵被无情弃，不能羞。",
                target="I wish to wed myself to him; abandoned, I bear no disgrace.",
            ),
            Segment(
                index=1,
                source="幕僚急忙进言。",
                target="The retainer hurried forward with counsel.",
            ),
            Segment(index=2, source="幕僚冷笑一声。", target="The retainer gave a cold laugh."),
            Segment(
                index=3, source="几位幕僚对视。", target="Several retainers exchanged glances."
            ),
            Segment(
                index=4,
                source="普通叙述二。",
                target="More ordinary narration without special roles.",
            ),
            Segment(
                index=5, source="普通叙述三。", target="Still more ordinary narration here too."
            ),
        ],
    )
    store.save_chapter(ch0)
    store.save_chapter(ch1)

    clusters = detect_mid_frequency_drift(
        [ch0, ch1],
        source_lang="zh",
        target_lang="en",
        existing_glossary_sources=set(),
        min_hits=5,
    )
    assert any(c.source == "幕僚" for c in clusters)

    cfg = Config(source_lang="zh", target_lang="en", llm=LLMConfig(preset="fake"))
    editor = SparseSemanticEditor(FakeClient(lambda m, op, jm: json.dumps({"patches": []})), cfg)
    svc = ConsistencySweepService(editor, source_lang="zh", target_lang="en")
    res = svc.run(store, chapter_indices=[0, 1], terms=[])
    assert res.quotes_unified == 1
    assert store.load_chapter(1).text_segments[0].target == ch0.text_segments[0].target


def test_pr30_review_regressions(tmp_path):
    from wenyi_core.storage.file import FileStorage

    # 1. Chinese corpus mining keeps names ending in 氏 / 君 (no Japanese honorific filter).
    zh_corpus = "王氏出面调解。王氏微笑。王氏叹气。王氏点头。孟尝君设宴。孟尝君大笑。孟尝君起身。"
    zh_cands = {c.source for c in mine_bible_candidates([], zh_corpus, source_lang="zh")}
    assert "王氏" in zh_cands
    assert "孟尝君" in zh_cands

    # 2. _cluster_candidate_rows deduplicates repeated stem hits on the same candidate.
    dup_cands = [
        (BibleCandidate("明月明月", 5, True, "", ""), "Mingyue", "Mingyue", False),
        (BibleCandidate("明月楼", 4, True, "", ""), "Mingyue Tower", "Moon Tower", True),
    ]
    rows = _cluster_candidate_rows(dup_cands)
    assert sum("明月明月" in r for r in rows) == 1

    # 3. Spoiler-safe gender guard does not clear male gender on mixed-gender dialogue about two people.
    mixed = _apply_spoiler_safe_gender_guard(
        [GlossaryTerm(source="张明", target="Zhang Ming", type="person", gender="male")],
        "张明看着李芳，他说她应该回家。",
        source_lang="zh",
    )
    assert mixed[0].gender == "male"

    # 4. post_normalize: URL mask leaves closing quote intact, em-dash \n unescapes, OLD Gao stays intact.
    url_cleaned, _ = normalize_chapter_latin_typography(
        [
            ("他说。", 'He said, "Hello."'),
            ("她说。", "“Visit https://example.com”"),
            ("诗。", "A spring outing—\\nApricot blossoms."),
        ],
        target_lang="en",
    )
    assert url_cleaned[1] == '"Visit https://example.com"'
    assert url_cleaned[2] == "A spring outing—\nApricot blossoms."

    norm_res, _ = normalize_minority_names(
        [("高老", "OLD Gao spoke, and Gao nodded.")],
        [GlossaryTerm(source="高老", target="Old Gao", type="person")],
        variants_by_source={"高老": ("Gao",)},
        target_lang="en",
    )
    assert norm_res[0] == "OLD Gao spoke, and Old Gao nodded."

    # 5. QuoteMemory: punctuation distinguishes We'll vs Well,, multi-quote doesn't cross-assign,
    #    and cross_chapter_spans order is deterministic.
    qm_punct = QuoteMemory.from_chapters(
        [
            (0, ["We'll remember everything forever.", "Well, remember everything forever."]),
        ]
    )
    assert qm_punct.repeated_count == 0

    qm_multi = QuoteMemory.from_chapters(
        [
            (
                0,
                [
                    'He said, "Never forget the oath of winter." and "Hold the northern wall tonight."'
                ],
            ),
            (1, ['She whispered, "Never forget the oath of winter."']),
        ]
    )
    qm_multi.observe(
        (0, 0),
        'He said, "Never forget the oath of winter." and "Hold the northern wall tonight."',
        'He said, "Never forget the winter oath."',
    )
    assert qm_multi.exact_match("Never forget the oath of winter.") is None
    assert (
        qm_multi.references_for_batch(['She whispered, "Never forget the oath of winter."']) == []
    )

    # 6. ConsistencySweepService protects segments with docx_styles metadata.
    store = FileStorage(str(tmp_path / "docx_guard"), create=True)
    ch0 = Chapter(
        index=0,
        title="Ch 0",
        segments=[
            Segment(
                index=0, source="妾拟将身嫁与，纵被无情弃，不能羞。", target="Canonical quote line."
            ),
        ],
    )
    ch1 = Chapter(
        index=1,
        title="Ch 1",
        segments=[
            Segment(
                index=0,
                source="妾拟将身嫁与，纵被无情弃，不能羞。",
                target="Protected DOCX styled line.",
                meta={"docx_styles": {"placements": []}},
            ),
        ],
    )
    store.save_chapter(ch0)
    store.save_chapter(ch1)
    cfg = Config(source_lang="zh", target_lang="en", llm=LLMConfig(preset="fake"))
    svc = ConsistencySweepService(
        SparseSemanticEditor(FakeClient(lambda m, op, jm: json.dumps({"patches": []})), cfg),
        source_lang="zh",
        target_lang="en",
    )
    res_docx = svc.run(store, chapter_indices=[0, 1], terms=[])
    assert res_docx.quotes_unified == 0
    assert store.load_chapter(1).text_segments[0].target == "Protected DOCX styled line."
