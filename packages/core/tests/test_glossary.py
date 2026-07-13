"""术语库 + 翻译记忆库测试。"""

from __future__ import annotations

import os
import tempfile
import unittest

from wenyi_core.glossary.store import (
    GlossaryStore,
    GlossaryTerm,
    TYPE_APPELLATION,
    TYPE_PERSON,
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
            GlossaryTerm(source="綾小路", target="绫小路", type=TYPE_PERSON,
                         gender="男", aliases=["綾小路くん"], reading="あやのこうじ"),
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

    def test_conflict_keeps_locked(self):
        self.store.upsert_term(
            GlossaryTerm(source="堀北", target="堀北", confidence="high"), chapter=0
        )
        self.store.lock_term("堀北")
        # 提出不同译法 → 应保留锁定译法并记冲突
        r = self.store.upsert_term(
            GlossaryTerm(source="堀北", target="掘北", confidence="medium"), chapter=1
        )
        self.assertEqual(r, "conflict")
        term = self.store.get_term("堀北")
        assert term is not None
        self.assertEqual(term.target, "堀北")
        self.assertEqual(len(self.store.open_conflicts()), 1)

    def test_conflict_overrides_low_confidence(self):
        self.store.upsert_term(
            GlossaryTerm(source="X", target="旧译", confidence="low"), chapter=0
        )
        r = self.store.upsert_term(
            GlossaryTerm(source="X", target="新译", confidence="high"), chapter=1
        )
        self.assertEqual(r, "updated")
        term = self.store.get_term("X")
        assert term is not None
        self.assertEqual(term.target, "新译")

    def test_translation_memory(self):
        self.store.add_tm("風が強かった。", "风很大。", chapter=1)
        self.assertEqual(self.store.tm_lookup("風が強かった。"), "风很大。")
        self.assertIsNone(self.store.tm_lookup("未登録"))

    def test_stats(self):
        self.store.upsert_term(GlossaryTerm(source="A", target="甲"))
        self.store.add_tm("a", "甲译")
        s = self.store.stats()
        self.assertEqual(s["terms"], 1)
        self.assertEqual(s["tm_entries"], 1)


if __name__ == "__main__":
    unittest.main()
