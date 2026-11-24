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
    select_voice_excerpts,
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
            # Simulate returning numeric index keys "0", "1", ...
            return json.dumps({"renderings": {"0": "Eastern Wu", "1": "Lin Yuan"}})
        return json.dumps(
            {
                "terms": [
                    {"source": "东吴", "target": "Eastern Wu", "type": "place"},
                    {"source": "林远", "target": "Lin Yuan", "type": "person", "gender": "male"},
                ],
                "conventions": ["Render 国主 consistently as 'the sovereign'."],
            }
        )

    agent = BookBibleAgent(FakeClient(handler), cfg)
    res = agent.compile_bible(
        ["东吴 and 林远 debate at court."],
        "东吴大军压境。东吴君臣震动。林远微笑。林远拱手。",
    )
    assert {t.source: t.target for t in res.terms} == {"东吴": "Eastern Wu", "林远": "Lin Yuan"}
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
        (BibleCandidate("青云楼", 4, True, "", ""), "Azure Pavilion", "Qingyun Tower", True),
        (
            BibleCandidate("青云阁", 3, True, "", ""),
            "Qingyun Hall",
            "Qingyun Hall",
            False,
        ),
        (BibleCandidate("林远", 9, True, "", ""), "Lin Yuan", "Lin Yuan", False),
    ]
    rows = _cluster_candidate_rows(cands)
    assert any("[Linked family: 青云]" in r for r in rows)

    terms = [
        GlossaryTerm(source="秦公子", target="Young Master Qin", type="person", gender="female"),
        GlossaryTerm(source="林青", target="Lin Qing", type="person", gender="female"),
    ]
    corpus = "秦公子微微一笑，他端起茶杯。后来秦公子敛衽一礼，她低声道。林青笑道，她很得意。"
    guarded = _apply_spoiler_safe_gender_guard(terms, corpus, source_lang="zh")
    by_src = {t.source: t for t in guarded}
    assert by_src["秦公子"].gender == ""
    assert "do not spoil" in by_src["秦公子"].note.lower()
    assert by_src["林青"].gender == "female"


def test_batch_risk_screener_catches_traps_and_escalates_sparse_editor():
    terms = [GlossaryTerm(source="青云楼", target="Qingyun Tower", type="place")]
    sources = [
        "不守北关以免后患，就先取西原以稳大局。",
        "林远来到青云楼前，吟道：“大漠孤烟直，长河落日圆。”",
        "有陈大人在，孤可以安枕无忧了。",
    ]
    drafts = [
        "Whether to leave the northern pass unguarded to spare ourselves trouble.",
        "Lin Yuan arrived at Azure Cloud Pavilion and recited, 'Lone smoke rises straight over the desert; the setting sun is round above the long river.'",
        "With Minister Chen here, the lone one could rest easy.",
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
    assert {"conditional_logic", "glossary_miss", "royal_pronoun"} <= codes
    assert "allusion" not in codes
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
        [("他笑道。", "I knew it — you agree.\n—Chronicles of Eastern Wu")],
        target_lang="en",
    )
    assert em_cleaned[0] == "I knew it—you agree.\n—Chronicles of Eastern Wu"

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
        (BibleCandidate("青云青云", 5, True, "", ""), "Qingyun", "Qingyun", False),
        (BibleCandidate("青云楼", 4, True, "", ""), "Qingyun Tower", "Cloud Tower", True),
    ]
    rows = _cluster_candidate_rows(dup_cands)
    assert sum("青云青云" in r for r in rows) == 1

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


def test_batch_risk_catches_phantom_entity_and_pronoun_flip_without_book_traps():
    terms = [
        GlossaryTerm(source="河洛", target="Heluo", type="place"),
        GlossaryTerm(source="东吴", target="Eastern Wu", type="place"),
    ]
    sources = [
        "若是将军亲自领兵，便能稳守北原，护我东吴。",
        "她女扮男装混入营中，乔装成年轻书生后便径直走上高台。",
        "我答应替他查清此事，绝不食言。",
        "大军稳守河洛，保我东吴安宁。",
    ]
    drafts = [
        "If the general leads the army, we can hold Heluo firmly and shield Eastern Wu.",
        "Disguised in men's robes as a young scholar, he walked straight onto the platform while she adjusted her sleeves.",
        "I promised to look into this matter for him and would never break my word.",
        "The army held Heluo firmly and kept Eastern Wu at peace.",
    ]
    report = assess_batch_risk(
        sources,
        drafts,
        source_lang="zh",
        target_lang="en",
        glossary_terms=terms,
    )
    by_idx = {(a.index, a.code) for a in report.alerts}
    assert (0, "phantom_entity") in by_idx
    assert (1, "pronoun_consistency") in by_idx
    # Removed book-specific agency trap must not fire.
    assert (2, "agency_direction") not in by_idx
    # Paragraph 3 genuinely contains 河洛 in source, so no false phantom_entity alert.
    assert (3, "phantom_entity") not in by_idx
    assert report.should_escalate is True

    # Ungrammatical third-person "the sir" calque triggers calque_or_idiom escalation.
    sir_report = assess_batch_risk(
        ["不要惊动先生。"],
        ["Let us not disturb the sir."],
        source_lang="zh",
        target_lang="en",
    )
    assert sir_report.should_escalate is True
    assert any(a.code == "calque_or_idiom" for a in sir_report.alerts)


def test_verse_segment_and_block_detection_ignore_modern_prose_and_isolated_lines():
    from wenyi_core.ingest.segmenter import is_verse_segment, verse_block_indices

    # Balanced modern prose (Simplified + Traditional) and 4-char pairs must not be flagged.
    for prose in (
        "悬崖边缘，横尸遍野！",
        "他耐得住寂寞，甚至享受寂寞。",
        "天色渐暗，微风轻拂。",
        "你放心吧，我明白的。",
        "他們向前走，這條路很長。",
        "你們看著吧，這事還沒完。",
    ):
        assert is_verse_segment(prose) is False

    # Classical 5- and 7-char shi couplets and multi-line stanzas (including >48 chars) qualify.
    assert is_verse_segment("大漠孤烟直，长河落日圆。") is True
    assert is_verse_segment("两个黄鹂鸣翠柳，一行白鹭上青天。") is True
    assert is_verse_segment("床前明月光，\n疑是地上霜。") is True
    eight_line_poem = "\n".join(
        [
            "两个黄鹂鸣翠柳，",
            "一行白鹭上青天。",
            "窗含西岭千秋雪，",
            "门泊东吴万里船。",
            "千山鸟飞绝，",
            "万径人踪灭。",
            "孤舟蓑笠翁，",
            "独钓寒江雪。",
        ]
    )
    assert len(eight_line_poem) > 48
    assert is_verse_segment(eight_line_poem) is True
    assert verse_block_indices([eight_line_poem]) == {0}

    # Hard-wrapped multi-line narrative prose (>48 chars) must NOT be misclassified as verse.
    wrapped_prose_3 = (
        "他推开木门走进昏暗的屋子，桌上点着一盏油灯，\n"
        "灯芯微微跳动，把墙上的影子拉得很长很长，\n"
        "窗外传来几声犬吠，很快又归于沉寂。"
    )
    wrapped_prose_4 = (
        "清晨的薄雾还没有完全散去，石板路上泛着湿气，\n"
        "几个挑着担子的小贩已经沿着长街缓缓走来，\n"
        "茶馆的伙计刚卸下门板，便听见远处传来一阵脚步声，\n"
        "众人抬头望去，只见一队车马正从巷口转入大街。"
    )
    assert len(wrapped_prose_3) > 48
    assert is_verse_segment(wrapped_prose_3) is False
    assert is_verse_segment(wrapped_prose_4) is False
    assert verse_block_indices([wrapped_prose_3, wrapped_prose_4]) == set()

    # Two isolated couplets separated by prose must NOT form a verse block or force \n splits.
    scattered_sources = [
        "大漠孤烟直，长河落日圆。",
        "他在屋里坐了很久，才慢慢站起身来。",
        "两个黄鹂鸣翠柳，一行白鹭上青天。",
    ]
    scattered_drafts = [
        "Lone smoke rises straight in the desert; the setting sun is round above the long river.",
        "He sat in the room for a long while before slowly rising.",
        "Two orioles sing among green willows; a line of egrets climbs the blue sky.",
    ]
    assert verse_block_indices(scattered_sources) == set()
    scattered_report = assess_batch_risk(
        scattered_sources, scattered_drafts, source_lang="zh", target_lang="en"
    )
    assert not any(a.code == "unlineated_verse" for a in scattered_report.alerts)

    # Contiguous multi-couplet poems and explicit multi-line stanzas DO require line breaks.
    contiguous_sources = [
        "两个黄鹂鸣翠柳，一行白鹭上青天。",
        "窗含西岭千秋雪，门泊东吴万里船。",
    ]
    contiguous_drafts = [
        "Two orioles sing among green willows, a line of egrets climbs the blue sky.",
        "The window frames the western peak's ancient snow, boats from Wu moor by the gate.",
    ]
    assert verse_block_indices(contiguous_sources) == {0, 1}
    contiguous_report = assess_batch_risk(
        contiguous_sources, contiguous_drafts, source_lang="zh", target_lang="en"
    )
    assert any(a.code == "unlineated_verse" for a in contiguous_report.alerts)
    assert verse_block_indices(["床前明月光，\n疑是地上霜。"]) == {0}


def test_book_bible_captures_divergent_titles_and_compiles_voice_exemplars():
    cfg = Config(source_lang="zh", target_lang="en", llm=LLMConfig(preset="fake"))
    probe_call = 0
    seen_adjudicate_user = ""

    def handler(messages, operation, json_mode):
        nonlocal probe_call, seen_adjudicate_user
        sys_msg = messages[0]["content"]
        user_msg = messages[-1]["content"]
        if "terminology prober" in sys_msg:
            probe_call += 1
            if probe_call == 1:
                return json.dumps({"renderings": {"东吴": "Eastern Wu", "国主": "the sovereign"}})
            return json.dumps({"renderings": {"东吴": "Eastern Wu", "国主": "the king"}})
        if "Book Bible" in sys_msg:
            seen_adjudicate_user = user_msg
            return json.dumps(
                {
                    "terms": [{"source": "东吴", "target": "Eastern Wu", "type": "place"}],
                    "conventions": ["Render 国主 consistently as 'the sovereign'."],
                }
            )
        if "voice calibration" in sys_msg:
            return json.dumps(
                {
                    "exemplars": {
                        "0": "Autumn rain pattered against the eaves of Qingyun Tower.",
                        "1": '"Wait a moment," he said with a wry smile.',
                    }
                }
            )
        return "{}"

    fake = FakeClient(handler)
    agent = BookBibleAgent(fake, cfg)
    corpus = "东吴国主微恙。东吴国主召见群臣。国主问政。国主叹息。东吴朝野震动。国主点头。"
    res = agent.compile_bible(["东吴 and 国主 meet at court."], corpus)
    assert "Divergent recurring titles" in seen_adjudicate_user
    assert "国主" in seen_adjudicate_user
    assert dict(res.divergent_titles)["国主"] == ("the sovereign", "the king")
    assert all(t.source != "国主" for t in res.terms)

    # Tiny stub text yields no excerpts; substantial corpus yields narration + dialogue exemplars.
    assert select_voice_excerpts("短句。\n又一句。", source_lang="zh") == []
    rich_corpus = "\n".join(
        [
            "秋雨细细打在青云楼的飞檐上，长街间却依旧车水马龙，往来文士议论着今科秋试的榜单与朝中局势，气氛既热闹又暗藏机锋。",
            "他放下折扇，微微笑道：“阁下此言差矣，若我东吴一味退让，强敌岂会因此止步？与其坐以待毙，不如先发制人，方能为社稷争得数十年喘息之机。”",
            "夜深人静之时，我独自站在廊下望着一弯冷月，想起白天宴席上的唇枪舌剑，只觉得人心难测，步步皆是险棋，稍有不慎便会万劫不复。",
        ]
    )
    excerpts = select_voice_excerpts(rich_corpus, source_lang="zh")
    assert len(excerpts) == 3
    exemplars = agent.compile_voice_exemplars(
        excerpts[:2],
        terms=res.terms,
        conventions=res.conventions,
    )
    assert len(exemplars) == 2
    assert fake.calls[-1]["thinking_boost"] == 1


def test_consistency_sweep_filters_function_noise_and_patches_confirmed_title_drift(tmp_path):
    from wenyi_core.storage.file import FileStorage

    store = FileStorage(str(tmp_path), create=True)
    ch0 = Chapter(
        index=0,
        title="Ch 0",
        segments=[
            Segment(
                index=0, source="国主已经下旨。", target="The sovereign already issued a decree."
            ),
            Segment(index=1, source="国主已经召见。", target="The sovereign already summoned him."),
            Segment(
                index=2, source="国主问政。", target="The sovereign asked about state affairs."
            ),
            Segment(
                index=3, source="普通叙述甲。", target="Ordinary narration alpha continues here."
            ),
            Segment(
                index=4, source="普通叙述乙。", target="Ordinary narration beta continues here."
            ),
        ],
    )
    ch1 = Chapter(
        index=1,
        title="Ch 1",
        segments=[
            Segment(
                index=0, source="国主已经回宫。", target="The king once returned to the palace."
            ),
            Segment(index=1, source="国主已经赏赐。", target="The king once bestowed rewards."),
            Segment(index=2, source="国主大喜。", target="The king was overjoyed."),
            Segment(
                index=3, source="普通叙述丙。", target="Ordinary narration gamma continues here."
            ),
            Segment(
                index=4, source="普通叙述丁。", target="Ordinary narration delta continues here."
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
        extra_watch_sources={"国主": ("the sovereign", "the king")},
    )
    cluster_sources = {c.source for c in clusters}
    assert "国主" in cluster_sources
    # Chinese adverb noise must be ignored.
    assert "已经" not in cluster_sources

    def handler(messages, operation, json_mode):
        sys_msg = messages[0]["content"]
        if "book consistency editor" in sys_msg:
            return json.dumps({"decisions": {"国主": "the sovereign"}})
        return json.dumps(
            {
                "patches": [
                    {
                        "index": 0,
                        "reason": "title_consistency",
                        "text": "The sovereign once returned to the palace.",
                    },
                    {
                        "index": 1,
                        "reason": "title_consistency",
                        "text": "The sovereign once bestowed rewards.",
                    },
                    {
                        "index": 2,
                        "reason": "title_consistency",
                        "text": "The sovereign was overjoyed.",
                    },
                ]
            }
        )

    cfg = Config(source_lang="zh", target_lang="en", llm=LLMConfig(preset="fake"))
    editor = SparseSemanticEditor(FakeClient(handler), cfg)
    svc = ConsistencySweepService(editor, source_lang="zh", target_lang="en")
    res = svc.run(
        store,
        chapter_indices=[0, 1],
        terms=[],
        divergent_titles={"国主": ("the sovereign", "the king")},
    )
    assert res.drift_patches_applied == 3
    ch1_saved = store.load_chapter(1)
    assert all("sovereign" in (s.target or "") for s in ch1_saved.text_segments[:3])


def test_consistency_sweep_catches_and_patches_watched_title_casing_split(tmp_path):
    from wenyi_core.storage.file import FileStorage

    # 1. Book Bible watches high-frequency agreed generic title (count >= 6), skipping bare pronouns.
    cfg = Config(source_lang="zh", target_lang="en", llm=LLMConfig(preset="fake"))
    agent = BookBibleAgent(
        FakeClient(
            lambda m, op, jm: (
                json.dumps({"renderings": {"国主": "the King", "大人": "sir"}})
                if "terminology prober" in m[0]["content"]
                else json.dumps({"terms": [], "conventions": ["Render 国主 as 'the King'."]})
            )
        ),
        cfg,
    )
    corpus = "国主微笑。国主下旨。国主召见。国主沉思。国主问政。国主点头。大人请坐。大人微笑。大人点头。大人叹气。大人起身。大人告退。"
    res_b = agent.compile_bible(["国主 speaks with 大人."], corpus)
    watched = dict(res_b.divergent_titles)
    assert "国主" in watched
    assert "大人" not in watched

    # 2. Case-sensitive mid-sentence split ("the King" vs "the king") is detected and patched.
    store = FileStorage(str(tmp_path / "casing"), create=True)
    ch0 = Chapter(
        index=0,
        title="Ch 0",
        segments=[
            Segment(index=0, source="国主下旨。", target="At dawn, the King issued a decree."),
            Segment(index=1, source="国主召见。", target="Soon, the King summoned his council."),
            Segment(index=2, source="国主问政。", target="Later, the King asked about Shu."),
            Segment(
                index=3, source="普通叙述甲。", target="Ordinary narration alpha continues here."
            ),
            Segment(
                index=4, source="普通叙述乙。", target="Ordinary narration beta continues here."
            ),
        ],
    )
    ch1 = Chapter(
        index=1,
        title="Ch 1",
        segments=[
            Segment(index=0, source="国主回宫。", target="At dusk, the king returned to court."),
            Segment(index=1, source="国主赏赐。", target="Quietly, the king bestowed rewards."),
            Segment(
                index=2, source="普通叙述丙。", target="Ordinary narration gamma continues here."
            ),
            Segment(
                index=3, source="普通叙述丁。", target="Ordinary narration delta continues here."
            ),
            Segment(index=4, source="普通叙述戊。", target="Ordinary narration epsilon continues."),
        ],
    )
    store.save_chapter(ch0)
    store.save_chapter(ch1)
    clusters = detect_mid_frequency_drift(
        [ch0, ch1],
        source_lang="zh",
        target_lang="en",
        existing_glossary_sources=set(),
        extra_watch_sources={"国主": ("the King",)},
    )
    assert any(
        c.source == "国主" and {t for t, _ in c.competing_targets} == {"King", "king"}
        for c in clusters
    )

    def handler(messages, operation, json_mode):
        if "book consistency editor" in messages[0]["content"]:
            return json.dumps({"decisions": {"国主": "the King"}})
        return json.dumps(
            {
                "patches": [
                    {
                        "index": 0,
                        "reason": "title_casing",
                        "text": "At dusk, the King returned to court.",
                    },
                    {
                        "index": 1,
                        "reason": "title_casing",
                        "text": "Quietly, the King bestowed rewards.",
                    },
                ]
            }
        )

    svc = ConsistencySweepService(
        SparseSemanticEditor(FakeClient(handler), cfg), source_lang="zh", target_lang="en"
    )
    res_s = svc.run(
        store,
        chapter_indices=[0, 1],
        terms=[],
        divergent_titles={"国主": ("the King",)},
    )
    assert res_s.drift_patches_applied == 2
    assert "the King" in (store.load_chapter(1).text_segments[0].target or "")


def test_pr31_review_regressions(tmp_path):
    import pytest
    from wenyi_core.storage.file import FileStorage

    cfg = Config(source_lang="zh", target_lang="en", llm=LLMConfig(preset="fake"))

    # 1. Title-only Book Bible adjudication runs even when proper-noun candidates list is empty.
    adjudicate_called = 0

    def title_only_handler(messages, operation, json_mode):
        nonlocal adjudicate_called
        if "terminology prober" in messages[0]["content"]:
            return json.dumps({"renderings": {"国主": "the king"}})
        adjudicate_called += 1
        return json.dumps({"terms": [], "conventions": ["Render 国主 as 'the king'."]})

    agent = BookBibleAgent(FakeClient(title_only_handler), cfg)
    res_b = agent.compile_bible(
        ["国主 presides."],
        "国主微笑。国主下旨。国主召见。国主沉思。国主问政。国主点头。",
    )
    assert adjudicate_called == 1
    assert res_b.conventions == ("Render 国主 as 'the king'.",)

    # 2. Voice calibration propagates resumable provider interrupts so bible cache is never poisoned.
    def outage_handler(messages, operation, json_mode):
        raise TimeoutError("provider temporary outage")

    outage_agent = BookBibleAgent(FakeClient(outage_handler), cfg)
    with pytest.raises(TimeoutError):
        outage_agent.compile_voice_exemplars(["段一长文。", "段二长文。"], terms=())

    # 3. Ordinary two-character sentence ("He took her hand, and she smiled.") is NOT flagged as pronoun flip.
    normal_report = assess_batch_risk(
        ["他握着她的手，她微微一笑。"],
        ["He took her hand, and she smiled."],
        source_lang="zh",
        target_lang="en",
    )
    assert all(a.code != "pronoun_consistency" for a in normal_report.alerts)

    # 4. Lowercase canonical drift decision ("the king") still patches mid-sentence "the King".
    store = FileStorage(str(tmp_path / "lower_canon"), create=True)
    store.save_chapter(
        Chapter(
            index=0,
            title="Ch 0",
            segments=[
                Segment(index=0, source="国主下旨。", target="At dawn, the King issued a decree."),
                Segment(index=1, source="国主召见。", target="Soon, the King summoned his court."),
                Segment(
                    index=2, source="普通叙述甲。", target="Ordinary narration alpha continues."
                ),
                Segment(
                    index=3, source="普通叙述乙。", target="Ordinary narration beta continues."
                ),
                Segment(
                    index=4, source="普通叙述丙。", target="Ordinary narration gamma continues."
                ),
            ],
        )
    )
    store.save_chapter(
        Chapter(
            index=1,
            title="Ch 1",
            segments=[
                Segment(index=0, source="国主回宫。", target="At dusk, the king returned home."),
                Segment(index=1, source="国主赏赐。", target="Quietly, the king bestowed gold."),
                Segment(
                    index=2, source="普通叙述丁。", target="Ordinary narration delta continues."
                ),
                Segment(
                    index=3, source="普通叙述戊。", target="Ordinary narration epsilon goes on."
                ),
                Segment(
                    index=4, source="普通叙述己。", target="Ordinary narration zeta continues."
                ),
            ],
        )
    )

    def lower_handler(messages, operation, json_mode):
        if "book consistency editor" in messages[0]["content"]:
            return json.dumps({"decisions": {"国主": "the king"}})
        return json.dumps(
            {
                "patches": [
                    {
                        "index": 0,
                        "reason": "title_casing",
                        "text": "At dawn, the king issued a decree.",
                    },
                    {
                        "index": 1,
                        "reason": "title_casing",
                        "text": "Soon, the king summoned his court.",
                    },
                ]
            }
        )

    svc = ConsistencySweepService(
        SparseSemanticEditor(FakeClient(lower_handler), cfg), source_lang="zh", target_lang="en"
    )
    res_low = svc.run(
        store,
        chapter_indices=[0, 1],
        terms=[],
        divergent_titles={"国主": ("the king",)},
    )
    assert res_low.drift_patches_applied == 2
    assert "the king" in (store.load_chapter(0).text_segments[0].target or "")
