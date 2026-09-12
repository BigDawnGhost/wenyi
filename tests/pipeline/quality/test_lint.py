"""确定性 lint 层单测：quote_loss / number_mismatch / term_miss / untranslated
+ 复用的 length_flags。全部纯函数，零 LLM。"""

from __future__ import annotations

import unittest

from trans_novel.glossary.store import GlossaryTerm
from trans_novel.pipeline.quality import lint_targets, polish_gate


def _types(issues, index=None):
    if index is None:
        return {i.type for i in issues}
    return {i.type for i in issues if i.index == index}


class TestQuoteLoss(unittest.TestCase):
    def test_flags_when_quotes_dropped(self):
        issues = lint_targets(
            ["「おはよう」と堀北が言った。"], ["早上好，堀北说道。"], src_lang="ja"
        )
        self.assertIn("quote_loss", _types(issues, 0))

    def test_passes_when_smart_quotes_kept(self):
        issues = lint_targets(["“Good morning,” she said."], ["“早上好，”她说道。"], src_lang="en")
        self.assertNotIn("quote_loss", _types(issues, 0))

    def test_passes_when_target_uses_cjk_corner_quotes(self):
        """译文用「」也算保留（不强制要求弯引号）。"""
        issues = lint_targets(
            ["「おはよう」と堀北が言った。"], ["「早上好」堀北说道。"], src_lang="ja"
        )
        self.assertNotIn("quote_loss", _types(issues, 0))

    def test_no_flag_when_source_has_no_quotes(self):
        issues = lint_targets(["今日は晴れです。"], ["今天天气晴朗。"], src_lang="ja")
        self.assertNotIn("quote_loss", _types(issues, 0))

    def test_single_quote_char_not_enough(self):
        # 源侧只出现 1 个引号字符（残缺配对），不构成"含直接引语"的证据 → 不 flag
        issues = lint_targets(["He said “hi."], ["他说嗨。"], src_lang="en")
        self.assertNotIn("quote_loss", _types(issues, 0))


class TestNumberMismatchBenchmarks(unittest.TestCase):
    """规格 §1b 五个基准案例。"""

    def test_thirty_miles_must_flag(self):
        issues = lint_targets(["He walked thirty miles."], ["他走了三英里。"], src_lang="en")
        self.assertIn("number_mismatch", _types(issues, 0))

    def test_dollar_amount_scaled_to_wan_must_pass(self):
        issues = lint_targets(["It cost $25,000."], ["花费2.5万美元。"], src_lang="en")
        self.assertNotIn("number_mismatch", _types(issues, 0))

    def test_couple_of_weeks_must_pass(self):
        issues = lint_targets(["A couple of weeks passed."], ["过了两三周。"], src_lang="en")
        self.assertNotIn("number_mismatch", _types(issues, 0))

    def test_year_1958_must_pass(self):
        issues = lint_targets(["It happened in 1958."], ["发生在1958年。"], src_lang="en")
        self.assertNotIn("number_mismatch", _types(issues, 0))

    def test_forty_steps_must_pass(self):
        issues = lint_targets(
            ["He climbed the Forty Steps."], ["他爬上了四十级台阶。"], src_lang="en"
        )
        self.assertNotIn("number_mismatch", _types(issues, 0))


class TestNumberMismatchExtra(unittest.TestCase):
    def test_small_number_below_threshold_not_flagged(self):
        # v<4 不判定（noise 太多，如章节号/序号误伤）
        issues = lint_targets(["He has three cats."], ["他养了猫。"], src_lang="en")
        self.assertNotIn("number_mismatch", _types(issues, 0))

    def test_target_extra_numbers_not_flagged(self):
        # 译文多出数值（意译增补）不 flag
        issues = lint_targets(
            ["He is forty years old."], ["他四十岁了，看起来像三十岁。"], src_lang="en"
        )
        self.assertNotIn("number_mismatch", _types(issues, 0))

    def test_ordinal_words_not_parsed(self):
        # 序数词不进值集合：first 不解析为 1，故不产生源侧数值，无需判定
        issues = lint_targets(["He finished first."], ["他第一个完成的。"], src_lang="en")
        self.assertNotIn("number_mismatch", _types(issues, 0))

    def test_arabic_number_mismatch_in_target(self):
        issues = lint_targets(["The record was 1958."], ["纪录是1968年。"], src_lang="en")
        self.assertIn("number_mismatch", _types(issues, 0))


class TestTermMiss(unittest.TestCase):
    def test_locked_term_missing_in_target_flags(self):
        term = GlossaryTerm(source="堀北", target="堀北", type="人物", locked=True)
        issues = lint_targets(
            ["堀北が振り返った。"], ["她转过身来。"], locked_terms=[term], src_lang="ja"
        )
        self.assertIn("term_miss", _types(issues, 0))

    def test_locked_term_present_in_target_passes(self):
        term = GlossaryTerm(source="堀北", target="堀北", type="人物", locked=True)
        issues = lint_targets(
            ["堀北が振り返った。"], ["堀北转过身来。"], locked_terms=[term], src_lang="ja"
        )
        self.assertNotIn("term_miss", _types(issues, 0))

    def test_word_boundary_prevents_false_positive(self):
        """latin 词边界：source 含 "Chang" 不应命中 "Changing"。"""
        term = GlossaryTerm(source="Chang", target="常先生", type="人物", locked=True)
        issues = lint_targets(
            ["Changing his mind was hard."],
            ["他很难改变主意。"],
            locked_terms=[term],
            src_lang="en",
        )
        self.assertNotIn("term_miss", _types(issues, 0))

    def test_word_boundary_matches_whole_word(self):
        term = GlossaryTerm(source="Chang", target="常先生", type="人物", locked=True)
        issues = lint_targets(
            ["Everyone said Chang walked in."],
            ["他走了进来。"],
            locked_terms=[term],
            src_lang="en",
        )
        self.assertIn("term_miss", _types(issues, 0))

    def test_unlocked_term_ignored(self):
        # 未传入的（即未锁定）术语不参与判定：调用方只传 locked=1 的词条
        issues = lint_targets(["堀北が振り返った。"], ["她转过身来。"], src_lang="ja")
        self.assertNotIn("term_miss", _types(issues, 0))

    def test_empty_target_term_not_flagged(self):
        term = GlossaryTerm(source="堀北", target="", type="人物", locked=True)
        issues = lint_targets(
            ["堀北が振り返った。"], ["她转过身来。"], locked_terms=[term], src_lang="ja"
        )
        self.assertNotIn("term_miss", _types(issues, 0))


class TestTermMissExemptions(unittest.TestCase):
    """term_miss 的两条确定性豁免（依据对剩余 129 条结果的人工归因：E1 命中 72 条，
    E2 命中 14 条，其余 43 条确为问题，不予豁免）。"""

    def test_e1_partial_target_hit_passes(self):
        """部分译名豁免：锁定「乔赛亚·蔡尔德」，译文只写「蔡尔德」也算已译。"""
        term = GlossaryTerm(source="Josiah", target="乔赛亚·蔡尔德", type="人物", locked=True)
        issues = lint_targets(
            ["Josiah walked into the room."],
            ["蔡尔德走进房间。"],
            locked_terms=[term],
            src_lang="en",
        )
        self.assertNotIn("term_miss", _types(issues, 0))

    def test_e1_no_separator_still_flags(self):
        """无分隔符的译名无法拆分，沿用原有行为：译文缺少完整译名时仍报告问题
        （源文特意避开句首，以排除 E2 干扰，并单独验证 E1 不会误判）。"""
        term = GlossaryTerm(source="Truman", target="杜鲁门", type="人物", locked=True)
        issues = lint_targets(
            ["Everyone recognized Truman immediately."],
            ["大家立刻认出了他。"],
            locked_terms=[term],
            src_lang="en",
        )
        self.assertIn("term_miss", _types(issues, 0))

    def test_e2_sentence_initial_only_passes(self):
        """句首歧义单词豁免：误录为人名的条目 Any→安尼；源文中的 Any 只出现在句首。"""
        term = GlossaryTerm(source="Any", target="安尼", type="人物", locked=True)
        issues = lint_targets(
            ["Any reduction of interest is welcome."],
            ["任何利息减少都受欢迎。"],
            locked_terms=[term],
            src_lang="en",
        )
        self.assertNotIn("term_miss", _types(issues, 0))

    def test_e2_non_initial_match_still_flags(self):
        """存在非句首命中则不豁免：the Law of Unintended Consequences 中的 Law。"""
        term = GlossaryTerm(source="Law", target="约翰·劳", type="人物", locked=True)
        issues = lint_targets(
            ["History remembers the Law of Unintended Consequences."],
            ["这不受欢迎。"],
            locked_terms=[term],
            src_lang="en",
        )
        self.assertIn("term_miss", _types(issues, 0))

    def test_e2_multi_word_source_not_exempted(self):
        """多词源（含空格）不走 E2：即使命中位于句首，译名缺失仍报。"""
        term = GlossaryTerm(source="St Bonaventure", target="圣文德", type="人物", locked=True)
        issues = lint_targets(
            ["St Bonaventure was born in Italy."],
            ["他出生在意大利。"],
            locked_terms=[term],
            src_lang="en",
        )
        self.assertIn("term_miss", _types(issues, 0))

    def test_cjk_source_not_exempted_by_e2(self):
        """CJK 源词不是仅含拉丁字母的单个 token；即使命中位于句首，也不适用 E2 豁免。"""
        term = GlossaryTerm(source="堀北", target="太郎", type="人物", locked=True)
        issues = lint_targets(
            ["堀北が振り返った。"], ["她转过身来。"], locked_terms=[term], src_lang="ja"
        )
        self.assertIn("term_miss", _types(issues, 0))

    def test_exact_hit_still_passes(self):
        """回归验证原有的精确子串匹配逻辑：出现完整译名时即通过。"""
        term = GlossaryTerm(source="Josiah", target="乔赛亚·蔡尔德", type="人物", locked=True)
        issues = lint_targets(
            ["Josiah walked into the room."],
            ["乔赛亚·蔡尔德走进房间。"],
            locked_terms=[term],
            src_lang="en",
        )
        self.assertNotIn("term_miss", _types(issues, 0))

    def test_e2_mid_sentence_quote_not_exempted(self):
        """句中引号不算句首：开引号出现在逗号和空格之后（如 said, “Law …），
        不满足"句末标点后接空白"的锚点条件，因此不予豁免，仍报告 term_miss。"""
        term = GlossaryTerm(source="Law", target="约翰·劳", type="人物", locked=True)
        issues = lint_targets(
            ['They invoked "Law" repeatedly.'],
            ["这不受欢迎。"],
            locked_terms=[term],
            src_lang="en",
        )
        self.assertIn("term_miss", _types(issues, 0))

    def test_e2_string_initial_quote_still_exempted(self):
        """字符串开头的开引号仍算句首的一部分：紧跟在开头之后。"""
        term = GlossaryTerm(source="Any", target="安尼", type="人物", locked=True)
        issues = lint_targets(
            ['"Any reduction of interest," he said.'],
            ["这不受欢迎。"],
            locked_terms=[term],
            src_lang="en",
        )
        self.assertNotIn("term_miss", _types(issues, 0))

    def test_e2_quote_after_comma_not_exempted(self):
        """句中逗号后接开引号（如 He said, “Law is king.”）不构成句首，
        不予豁免。"""
        term = GlossaryTerm(source="Law", target="约翰·劳", type="人物", locked=True)
        issues = lint_targets(
            ["He said, \u201cLaw is king.\u201d"],
            ["这不受欢迎。"],
            locked_terms=[term],
            src_lang="en",
        )
        self.assertIn("term_miss", _types(issues, 0))

    def test_e2_adjacent_quote_after_period_exempted(self):
        """相邻引语：句末标点和闭引号后有空白，随后出现的开引号仍属于下一句句首。"""
        term = GlossaryTerm(source="Any", target="安尼", type="人物", locked=True)
        issues = lint_targets(
            ["He replied, \u201cNo.\u201d \u201cAny reduction is welcome.\u201d"],
            ["这不受欢迎。"],
            locked_terms=[term],
            src_lang="en",
        )
        self.assertNotIn("term_miss", _types(issues, 0))


class TestUntranslated(unittest.TestCase):
    def test_identical_text_flags(self):
        issues = lint_targets(["Hello world."], ["Hello world."], src_lang="en")
        self.assertIn("untranslated", _types(issues, 0))

    def test_case_and_whitespace_insensitive_identity(self):
        issues = lint_targets(["Hello   World."], ["hello world."], src_lang="en")
        self.assertIn("untranslated", _types(issues, 0))

    def test_normal_chinese_translation_passes(self):
        issues = lint_targets(["Hello world."], ["你好，世界。"], src_lang="en")
        self.assertNotIn("untranslated", _types(issues, 0))

    def test_long_latin_run_verbatim_in_source_flags(self):
        latin = "a" * 45
        issues = lint_targets([f"prefix {latin} suffix"], [f"前缀 {latin} 后缀"], src_lang="en")
        self.assertIn("untranslated", _types(issues, 0))

    def test_latin_run_below_40_chars_not_flagged(self):
        latin = "a" * 39
        issues = lint_targets([f"prefix {latin} suffix"], [f"前缀 {latin} 后缀"], src_lang="en")
        self.assertNotIn("untranslated", _types(issues, 0))

    def test_disabled_when_source_is_chinese(self):
        issues = lint_targets(["你好世界"], ["你好世界"], src_lang="zh")
        self.assertNotIn("untranslated", _types(issues, 0))

    def test_numeric_heading_is_not_untranslated(self):
        issues = lint_targets(["15"], ["15"], src_lang="en")
        self.assertNotIn("untranslated", _types(issues, 0))

    def test_machine_literal_is_not_untranslated(self):
        literal = "{var=dc:http_errors:rate10m,job=webserver}"
        issues = lint_targets([literal], [literal], src_lang="en")
        self.assertNotIn("untranslated", _types(issues, 0))


class TestLengthFlagsReuse(unittest.TestCase):
    def test_empty_target_flags(self):
        issues = lint_targets(["这是一段有内容的原文用于测试。"], [""], src_lang="zh")
        self.assertIn("empty", _types(issues, 0))

    def test_too_short_target_flags(self):
        issues = lint_targets(
            ["这是一段有一定长度的原文，用来测试过短判定阈值。"], ["短"], src_lang="zh"
        )
        self.assertIn("too_short", _types(issues, 0))

    def test_too_long_target_flags(self):
        issues = lint_targets(
            ["短句。"], ["这是一段被大幅增译、明显超出原文长度比例的过长译文内容。"], src_lang="zh"
        )
        self.assertIn("too_long", _types(issues, 0))


class TestMultiSegment(unittest.TestCase):
    def test_indices_align_with_input_order(self):
        sources = ["今日は晴れです。", "「おはよう」と堀北が言った。"]
        targets = ["今天天气晴朗。", "早上好，堀北说道。"]
        issues = lint_targets(sources, targets, src_lang="ja")
        self.assertEqual(_types(issues, 0), set())
        self.assertIn("quote_loss", _types(issues, 1))

    def test_no_issues_on_clean_batch(self):
        sources = ["今日は晴れです。", "堀北が振り返った。"]
        targets = ["今天天气晴朗。", "她转过身来。"]
        self.assertEqual(lint_targets(sources, targets, src_lang="ja"), [])


class TestQuoteLossFixPass(unittest.TestCase):
    """回测修复：行首闭引号不触发 + 《》算保留。"""

    def test_leading_close_quote_not_flagged(self):
        # 句段切分产生的孤立后半句，行首是闭引号残段，不构成"含直接引语"的证据
        issues = lint_targets(
            ["」なんて言わないでください、と彼女は静かに言った。"],
            ["别这么说，她轻声说道。"],
            src_lang="ja",
        )
        self.assertNotIn("quote_loss", _types(issues, 0))

    def test_book_title_bracket_counts_as_kept(self):
        # 引题名转书名号《》是正确译法，不算丢引号
        issues = lint_targets(
            ["「戦争と平和」を読んだ。"], ["他读了《战争与和平》。"], src_lang="ja"
        )
        self.assertNotIn("quote_loss", _types(issues, 0))


class TestNumberMismatchFixPass(unittest.TestCase):
    """回测修复：组合数词/汉字数字串/年代等价。"""

    def test_hundred_and_combo(self):
        issues = lint_targets(
            ["The village had eight hundred and thirty-six residents in total."],
            ["这个村子总共有836名居民。"],
            src_lang="en",
        )
        self.assertNotIn("number_mismatch", _types(issues, 0))

    def test_decimal_billion_combo(self):
        issues = lint_targets(
            ["The company raised 11.8 billion dollars in the round."],
            ["该公司在这轮融资中筹集了118亿美元。"],
            src_lang="en",
        )
        self.assertNotIn("number_mismatch", _types(issues, 0))

    def test_cn_digit_literal_reading(self):
        issues = lint_targets(
            ["It happened in 1922, during the war."],
            ["这件事发生在一九二二年，正值战争期间。"],
            src_lang="en",
        )
        self.assertNotIn("number_mismatch", _types(issues, 0))

    def test_decade_equivalence(self):
        issues = lint_targets(
            ["This song was popular in the 1980s across the country."],
            ["这首歌在20世纪80年代红遍全国。"],
            src_lang="en",
        )
        self.assertNotIn("number_mismatch", _types(issues, 0))

    def test_century_equivalence(self):
        issues = lint_targets(
            ["The castle was built in the 1600s by a local lord."],
            ["这座城堡建于十七世纪，由当地领主所建。"],
            src_lang="en",
        )
        self.assertNotIn("number_mismatch", _types(issues, 0))

    def test_wildcard_year_prefix_match(self):
        issues = lint_targets(
            ["It happened around 1943 or so."], ["大概是一九四几年发生的事。"], src_lang="en"
        )
        self.assertNotIn("number_mismatch", _types(issues, 0))

    def test_compound_wan_yi_matches_trillion(self):
        issues = lint_targets(
            ["The fund reached 420 trillion dollars."], ["该基金达到420万亿。"], src_lang="en"
        )
        self.assertNotIn("number_mismatch", _types(issues, 0))

    def test_missing_24_still_flags(self):
        issues = lint_targets(["The fund reached 24 dollars."], ["该基金达到金额。"], src_lang="en")
        self.assertIn("number_mismatch", _types(issues, 0))


class TestUntranslatedFixPass(unittest.TestCase):
    def test_url_token_exempted(self):
        issues = lint_targets(
            ["https://example.com/page?id=123"], ["https://example.com/page?id=123"], src_lang="en"
        )
        self.assertNotIn("untranslated", _types(issues, 0))

    def test_isbn_token_exempted(self):
        issues = lint_targets(["ISBN-978-0-13-468599-1"], ["ISBN-978-0-13-468599-1"], src_lang="en")
        self.assertNotIn("untranslated", _types(issues, 0))


class TestTooShortEnHardGate(unittest.TestCase):
    """en 源 too_short 硬门槛：ratio<0.15 且 len(src)>=120 才 flag。"""

    SRC = (
        "This is a fairly long English sentence deliberately padded out with "
        "many extra filler words so its character count clears the threshold "
        "easily for this particular test case here now."
    )

    def test_moderate_ratio_not_flagged(self):
        target = "y" * int(len(self.SRC) * 0.2)
        issues = lint_targets([self.SRC], [target], src_lang="en")
        self.assertNotIn("too_short", _types(issues, 0))

    def test_extreme_ratio_flagged(self):
        target = "y" * int(len(self.SRC) * 0.1)
        issues = lint_targets([self.SRC], [target], src_lang="en")
        self.assertIn("too_short", _types(issues, 0))

    def test_non_en_keeps_default_threshold(self):
        # 非 en 源不受硬门槛限制，沿用 checks.length_flags 默认阈值 0.30
        src = "这是一段用来测试默认长度阈值的中文原文，长度不算很短。"
        issues = lint_targets([src], ["短"], src_lang="zh")
        self.assertIn("too_short", _types(issues, 0))


class TestNumberMismatchProductionFixtures(unittest.TestCase):
    """第二轮生产等价回测修复：13 个真实书籍 fixture（Chip_War / The_Wedding_People），
    覆盖乘数跨词误组合、组合残留组件、年份不参与组合、字母粘连伪影、decades 等价、
    zh 侧空格容忍、一半/X成 惯用比例词。逐条断言不再误报（1 条除外，见文末说明）。"""

    def test_ch11_year_excluded_from_range_multiplier(self):
        # "...$500,000 in 1958 to $21 million..." 曾把 1958 误套進 "to $21 million"
        # 的乘数回填，产出幻影值 1958000000；年份现在永不参与组合。
        src = (
            "Sales ballooned from $500,000 in 1958 to $21 million two years later, "
            "helped by one thousand new employees."
        )
        tgt = "销售额从1958年的50万美元飙升至两年后的2100万美元，得益于千名新员工。"
        issues = lint_targets([src], [tgt], src_lang="en")
        self.assertNotIn("number_mismatch", _types(issues, 0))

    def test_ch17_year_excluded_from_range_multiplier_billion(self):
        src = "Exports boomed from $600 million in 1965 to $60 billion around two decades later."
        tgt = "出口额从1965年的6亿美元飙升至大约二十年后的600亿美元。"
        issues = lint_targets([src], [tgt], src_lang="en")
        self.assertNotIn("number_mismatch", _types(issues, 0))

    def test_ch34_zh_space_before_scale_suffix(self):
        # "120 万" 数字与万之间有空格，须仍组合为 1200000
        src = "A small piece of silicon packed with 1.2 million microscopic switches."
        tgt = "一枚集成了 120 万个微型开关的硅片。"
        issues = lint_targets([src], [tgt], src_lang="en")
        self.assertNotIn("number_mismatch", _types(issues, 0))

    def test_ch40_2_glued_page_number_artifact_excluded(self):
        # "164Manufacturing" 数字与字母无空格粘连，是版式伪影非真实数量，不提取
        src = "The rise of the Taiwan Semiconductor 164Manufacturing Company was spectacular."
        tgt = "台积电的惊人崛起。"
        issues = lint_targets([src], [tgt], src_lang="en")
        self.assertNotIn("number_mismatch", _types(issues, 0))

    def test_ch40_7_decades_equivalence(self):
        src = "He hadn't visited since fleeing nearly four decades earlier."
        tgt = "他在近四十年前逃离后从未踏足。"
        issues = lint_targets([src], [tgt], src_lang="en")
        self.assertNotIn("number_mismatch", _types(issues, 0))

    def test_ch40_18_glued_number_artifact_excluded(self):
        src = "The company had a built-in advantage, 169improving its yield."
        tgt = "这家公司享有先天优势，能提高良率。"
        issues = lint_targets([src], [tgt], src_lang="en")
        self.assertNotIn("number_mismatch", _types(issues, 0))

    def test_ch41_14_teens_hundred_combo_no_residual(self):
        # "fifteen hundred" 须组合为 1500，且不残留 15 单独进入值集合
        src = "China had only fifteen hundred computers in the entire country."
        tgt = "中国仅有一千五百台计算机。"
        issues = lint_targets([src], [tgt], src_lang="en")
        self.assertNotIn("number_mismatch", _types(issues, 0))

    def test_ch49_2_glued_number_artifact_excluded(self):
        src = "He defended it as good for 216his health, or at least for his mood."
        tgt = "他辩称这一习惯有益于健康，至少有益于心情。"
        issues = lint_targets([src], [tgt], src_lang="en")
        self.assertNotIn("number_mismatch", _types(issues, 0))

    def test_ch50_4_percent_to_cheng_idiom(self):
        src = "Apple was making over 60 percent of all the world's profits."
        tgt = "苹果已拿下全球利润的逾六成。"
        issues = lint_targets([src], [tgt], src_lang="en")
        self.assertNotIn("number_mismatch", _types(issues, 0))

    def test_ch66_1_covid_hyphen_identifier_excluded(self):
        src = "It faced restrictions amid a tsunami of cases of COVID-19."
        tgt = "它面临着限制措施，新冠疫情病例激增。"
        issues = lint_targets([src], [tgt], src_lang="en")
        self.assertNotIn("number_mismatch", _types(issues, 0))

    def test_wedding_16_105_percent_to_half_idiom(self):
        src = "I know the shape and size and color of about fifty percent of the inhabitants."
        tgt = "随便哪个房间，里头大约一半人的样子我都门儿清。"
        issues = lint_targets([src], [tgt], src_lang="en")
        self.assertNotIn("number_mismatch", _types(issues, 0))

    def test_wedding_23_79_high_five_idiom_known_residual(self):
        # "high five"（击掌）里的 five 被朴素数词解析器当成数量词 5，属固有局限：
        # 无法通用地区分习语命名与真实计数，未在本轮规则覆盖范围内（Main 确认
        # ≤2 条残留可接受）。此处保留断言 IS flagged，作为已知限度的文档化记录，
        # 而非静默接受——回归测试若哪天此案例被修复，本用例会先失败提醒更新。
        src = "She gives him a tiny high five as though the big task of the day is over."
        tgt = "她轻轻跟他击了个掌，好像今天的大任务已经完成了。"
        issues = lint_targets([src], [tgt], src_lang="en")
        self.assertIn("number_mismatch", _types(issues, 0))


class TestPolishGateEvidence(unittest.TestCase):
    def test_rejection_retains_raw_and_proposal_lint_details(self):
        result = polish_gate(
            '"Hello," she said.',
            "“你好，”她说。",
            "你好，她说。",
            src_lang="en",
            normalize_punctuation=False,
        )

        self.assertFalse(result.accepted)
        self.assertEqual(result.checked_raw, "“你好，”她说。")
        self.assertEqual(result.checked_proposal, "你好，她说。")
        self.assertEqual(result.raw_issues, [])
        self.assertEqual(
            [(issue.type, issue.detail) for issue in result.proposal_issues],
            [("quote_loss", "原文含直接引语，译文丢失引号，必须保留成对引号")],
        )


if __name__ == "__main__":
    unittest.main()
