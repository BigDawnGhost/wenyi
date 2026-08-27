"""术语库测试。"""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from trans_novel.glossary.store import (
    TYPE_APPELLATION,
    TYPE_PERSON,
    GlossaryStore,
    GlossaryTerm,
    source_matches_text,
)


class TestGlossary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = GlossaryStore(os.path.join(self.tmp.name, "g.db"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_insert_and_lookup(self):
        r = self.store.upsert_term(
            GlossaryTerm(
                source="綾小路",
                target="绫小路",
                type=TYPE_PERSON,
                gender="男",
                aliases=["綾小路くん"],
                reading="あやのこうじ",
            ),
            chapter=0,
        )
        self.assertEqual(r, "inserted")
        t = self.store.get_term("綾小路")
        assert t is not None
        self.assertEqual(t.target, "绫小路")
        self.assertEqual(t.gender, "男")

    def test_terms_in_text_matches_alias(self):
        self.store.upsert_term(
            GlossaryTerm(source="綾小路", target="绫小路", aliases=["綾小路くん"])
        )
        hits = self.store.terms_in_text("「おはよう、綾小路くん」と堀北が言った。")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].source, "綾小路")

    def test_terms_in_text_normalizes_case_and_character_width(self):
        self.store.upsert_term(GlossaryTerm(source="OpenAI", target="开放人工智能"))
        self.store.upsert_term(GlossaryTerm(source="ＡＢＣ", target="ABC 组织"))

        hits = self.store.terms_in_text("openai 与 ABC")

        self.assertEqual(
            {term.source for term in hits},
            {"OpenAI", "ＡＢＣ"},
        )

    def test_ascii_source_match_respects_word_boundaries(self):
        self.assertTrue(source_matches_text("Ann", "Ann opened the door."))
        self.assertTrue(source_matches_text("ANN", "ann opened the door."))
        self.assertFalse(source_matches_text("Ann", "Anna opened the door."))

    def test_cyrillic_source_match_respects_word_boundaries(self):
        self.assertTrue(source_matches_text("гад", "Этот гад снова пришёл."))
        self.assertFalse(source_matches_text("гад", "Этот гадкий человек снова пришёл."))

    def test_kana_source_match_respects_kana_run_boundaries(self):
        # 短假名词嵌在同文字假名串中间时是更长单词的一部分，不应命中。
        self.assertFalse(source_matches_text("あんな", "あんなに急に変わる。"))
        self.assertFalse(source_matches_text("メイ", "メインストリートを歩く。"))
        self.assertFalse(source_matches_text("しょう", "しょうがないから行く。"))
        # 独立出现的假名词正常命中；中点「・」是分隔符而非片假名延续。
        self.assertTrue(source_matches_text("メイ", "「メイ、おはよう」"))
        self.assertTrue(source_matches_text("あんな", "「あんな！」と叫んだ。"))
        self.assertTrue(source_matches_text("メイ", "メイ・スミスは黙っていた。"))
        # 片假名词后跟平假名助词属正常接续。
        self.assertTrue(source_matches_text("メイ", "メイは立ち止まった。"))

    def test_kana_term_not_injected_from_longer_kana_run(self):
        self.store.upsert_term(
            GlossaryTerm(source="あんな", target="Anna", type=TYPE_PERSON)
        )
        self.store.upsert_term(GlossaryTerm(source="メイ", target="小梅", type=TYPE_PERSON))

        self.assertEqual(self.store.terms_in_text("あんなに慌てなくてもいい。"), [])
        self.assertEqual(self.store.terms_in_text("メイン料理を注文した。"), [])
        self.assertEqual(
            {term.source for term in self.store.terms_in_text("「あんな、メイが呼んでる」")},
            {"あんな", "メイ"},
        )

    def test_appellation_does_not_match_bare_name_alias(self):
        self.store.upsert_term(
            GlossaryTerm(
                source="夏帆ちゃん",
                target="小夏帆",
                type=TYPE_APPELLATION,
                aliases=["夏帆"],
            )
        )
        self.assertEqual(self.store.terms_in_text("夏帆は窓の外を見た。"), [])
        hits = self.store.terms_in_text("「夏帆ちゃん」と母親が言った。")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].source, "夏帆ちゃん")

    def test_recurring_terms_require_two_full_text_occurrences(self):
        terms = [
            GlossaryTerm(source="唯一术语", target="Unique"),
            GlossaryTerm(source="重复术语", target="Repeated"),
            GlossaryTerm(
                source="AliasCanonical",
                target="Alias",
                aliases=["别名"],
            ),
            GlossaryTerm(
                source="夏帆ちゃん",
                target="小夏帆",
                type=TYPE_APPELLATION,
                aliases=["夏帆"],
            ),
        ]
        corpus = "唯一术语。重复术语再次成为重复术语。别名先来，别名再来。夏帆出现两次，夏帆。"

        recurring = GlossaryStore.recurring_terms(terms, corpus)

        self.assertEqual(
            {term.source for term in recurring},
            {"重复术语", "AliasCanonical"},
        )

        overlapping_alias = GlossaryTerm(
            source="夏帆",
            target="Kaho",
            aliases=["夏帆ちゃん"],
        )
        self.assertEqual(
            GlossaryStore.recurring_terms(
                [overlapping_alias],
                "夏帆ちゃん只在全文出现一次。",
            ),
            [],
        )

        cyrillic = GlossaryTerm(source="гад", target="畜生")
        self.assertEqual(
            GlossaryStore.recurring_terms(
                [cyrillic],
                "Один гад ушёл, но гадкий человек остался гадким.",
            ),
            [],
        )
        self.assertEqual(
            GlossaryStore.recurring_terms(
                [cyrillic],
                "Один гад ушёл, затем другой гад пришёл.",
            ),
            [cyrillic],
        )

    def test_conflict_keeps_current_until_resolved(self):
        self.store.upsert_term(GlossaryTerm(source="堀北", target="堀北"), chapter=0)
        # 提交不同译法：保留当前译法并记录候选项。
        r = self.store.upsert_term(GlossaryTerm(source="堀北", target="掘北"), chapter=1)
        self.assertEqual(r, "conflict")
        term = self.store.get_term("堀北")
        assert term is not None
        self.assertEqual(term.target, "堀北")
        self.assertEqual(len(self.store.open_conflicts()), 1)

        self.assertTrue(self.store.resolve_term("堀北", "掘北"))
        self.store.mark_conflicts_resolved("堀北")
        term = self.store.get_term("堀北")
        assert term is not None
        self.assertEqual(term.target, "掘北")
        self.assertEqual(term.status, "ok")
        self.assertEqual(self.store.open_conflicts(), [])

    def test_same_target_conflicting_gender_is_flagged_not_overwritten(self):
        self.store.upsert_term(
            GlossaryTerm(source="白井", target="白井", type=TYPE_PERSON, gender="女"),
            chapter=0,
        )
        # 同译法但性别判断相左：保留现有事实并登记冲突，而非静默覆盖。
        r = self.store.upsert_term(
            GlossaryTerm(source="白井", target="白井", type=TYPE_PERSON, gender="男"),
            chapter=2,
        )
        self.assertEqual(r, "conflict")
        term = self.store.get_term("白井")
        assert term is not None
        self.assertEqual(term.gender, "女")
        self.assertEqual(term.status, "conflict")
        conflicts = self.store.open_conflicts()
        self.assertEqual(len(conflicts), 1)
        self.assertIn("性别冲突", conflicts[0]["note"])

        self.assertTrue(self.store.resolve_term("白井", "白井"))
        self.store.mark_conflicts_resolved("白井")
        term = self.store.get_term("白井")
        assert term is not None
        self.assertEqual(term.status, "ok")
        self.assertEqual(self.store.open_conflicts(), [])

    def test_same_target_same_or_empty_gender_stays_unchanged(self):
        self.store.upsert_term(
            GlossaryTerm(source="白井", target="白井", gender="女"),
            chapter=0,
        )
        r = self.store.upsert_term(GlossaryTerm(source="白井", target="白井", gender="女"))
        self.assertEqual(r, "unchanged")
        self.assertEqual(self.store.get_term("白井").status, "ok")
        # 空 gender 不视为冲突，其它字段照常补全。
        r = self.store.upsert_term(GlossaryTerm(source="白井", target="白井", note="班长"))
        self.assertEqual(r, "unchanged")
        term = self.store.get_term("白井")
        assert term is not None
        self.assertEqual(term.gender, "女")
        self.assertEqual(term.note, "班长")

    def test_unknown_gender_markers_are_normalized_at_store_entry(self):
        # Analyzer 会把 prompt 里的「未知」原样种入；未知不等于矛盾事实。
        self.store.upsert_term(GlossaryTerm(source="白井", target="白井", gender="未知"))
        term = self.store.get_term("白井")
        assert term is not None
        self.assertEqual(term.gender, "")

        self.store.upsert_term(
            GlossaryTerm(source="真昼", target="真昼", gender="女"),
            chapter=0,
        )
        r = self.store.upsert_term(
            GlossaryTerm(source="真昼", target="真昼", gender="不明"),
            chapter=1,
        )
        self.assertEqual(r, "unchanged")
        term = self.store.get_term("真昼")
        assert term is not None
        self.assertEqual(term.gender, "女")
        self.assertEqual(term.status, "ok")
        self.assertEqual(self.store.open_conflicts(), [])

    def test_legacy_unknown_gender_value_does_not_create_false_conflict(self):
        # v0.5.1 时代 Analyzer 把「未知」原样写入库（绕过入口归一模拟旧库）。
        self.store.conn.execute(
            """INSERT INTO glossary
               (source,target,reading,type,gender,aliases,first_chapter,note,
                status,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            ("白井", "白井", "", TYPE_PERSON, "未知", "[]", 0, "", "ok", time.time()),
        )
        self.store.conn.commit()

        r = self.store.upsert_term(
            GlossaryTerm(source="白井", target="白井", gender="女"),
            chapter=1,
        )
        self.assertEqual(r, "unchanged")
        term = self.store.get_term("白井")
        assert term is not None
        self.assertEqual(term.gender, "女")
        self.assertEqual(term.status, "ok")
        self.assertEqual(self.store.open_conflicts(), [])

    def test_resolve_term_treats_unknown_gender_marker_as_keep(self):
        self.store.upsert_term(GlossaryTerm(source="白井", target="白井", gender="女"))
        self.assertTrue(self.store.resolve_term("白井", "白井", gender="未知"))
        self.assertEqual(self.store.get_term("白井").gender, "女")

    def test_resolve_can_adjudicate_gender(self):
        self.store.upsert_term(
            GlossaryTerm(source="白井", target="白井", gender="女"),
            chapter=0,
        )
        self.store.upsert_term(
            GlossaryTerm(source="白井", target="白井", gender="男"),
            chapter=2,
        )
        # 不传 gender：确认现有性别，但目标译法与状态照常裁定。
        self.assertTrue(self.store.resolve_term("白井", "白井"))
        term = self.store.get_term("白井")
        assert term is not None
        self.assertEqual(term.gender, "女")
        self.assertEqual(term.status, "ok")

        # 传 gender：真正更新性别，冲突可随之关闭。
        self.store.upsert_term(
            GlossaryTerm(source="白井", target="白井", gender="男"),
            chapter=3,
        )
        self.assertTrue(self.store.resolve_term("白井", "白井", gender="男"))
        self.store.mark_conflicts_resolved("白井")
        term = self.store.get_term("白井")
        assert term is not None
        self.assertEqual(term.gender, "男")
        self.assertEqual(term.status, "ok")
        self.assertEqual(self.store.open_conflicts(), [])

    def test_hiragana_particle_continuation_is_a_known_precision_tradeoff(self):
        # 平假名键遇同文字假名延续一律不命中：无分词器时无法区分
        # 「ゆきは…」（名字+助词）与「あんなに」（更长单词）。本实现选择
        # precision over recall——宁可少注入一次，也不让错误事实进入
        # prompt。该规则作用于所有纯平假名词条，不只人物短名；
        # 改动此行为需要先引入分词。
        self.store.upsert_term(
            GlossaryTerm(source="ゆき", target="小雪", type=TYPE_PERSON)
        )
        self.assertEqual(self.store.terms_in_text("ゆきは歩いた。"), [])

    def test_concurrent_upserts_make_one_atomic_conflict_decision(self):
        path = os.path.join(self.tmp.name, "concurrent.db")
        initial = GlossaryStore(path)
        initial.close()
        barrier = threading.Barrier(2)

        def write(target: str) -> str:
            store = GlossaryStore(path)
            try:
                barrier.wait()
                return store.upsert_term(GlossaryTerm(source="Name", target=target), chapter=1)
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(write, ["译名甲", "译名乙"]))

        check = GlossaryStore(path)
        try:
            self.assertCountEqual(results, ["inserted", "conflict"])
            self.assertEqual(len(check.all_terms()), 1)
            self.assertEqual(len(check.open_conflicts()), 1)
        finally:
            check.close()

    def test_stats(self):
        self.store.upsert_term(GlossaryTerm(source="A", target="甲"))
        s = self.store.stats()
        self.assertEqual(s, {"terms": 1, "open_conflicts": 0})

    def test_all_terms_preserves_insert_order_not_type_source_sort(self):
        """入库先后决定 all_terms 顺序，避免新词插队打乱 prompt 前缀缓存。"""
        # 故意先插「乙」(type 术语)，再插「甲」(type 人物)：字母/类型序会变成 甲,乙。
        self.store.upsert_term(
            GlossaryTerm(source="乙", target="Yi", type="术语"),
            chapter=0,
        )
        self.store.upsert_term(
            GlossaryTerm(source="甲", target="Jia", type=TYPE_PERSON),
            chapter=0,
        )
        self.assertEqual(
            [term.source for term in self.store.all_terms()],
            ["乙", "甲"],
        )
        # 人工改定译法（或同 target 合并字段）不得改变入库位置。
        self.assertTrue(self.store.resolve_term("乙", "Yi-updated"))
        terms = self.store.all_terms()
        self.assertEqual([term.source for term in terms], ["乙", "甲"])
        self.assertEqual(terms[0].target, "Yi-updated")
        # 新词只能追加在末尾。
        self.store.upsert_term(
            GlossaryTerm(source="丙", target="Bing", type=TYPE_PERSON),
            chapter=1,
        )
        self.assertEqual(
            [term.source for term in self.store.all_terms()],
            ["乙", "甲", "丙"],
        )


if __name__ == "__main__":
    unittest.main()
