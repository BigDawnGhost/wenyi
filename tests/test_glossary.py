"""术语库测试。"""

from __future__ import annotations

import os
import tempfile
import threading
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
