"""Glossary tests."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from wenyi_core.glossary.store import (
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
                gender="male",
                aliases=["綾小路くん"],
                reading="あやのこうじ",
            ),
            chapter=0,
        )
        self.assertEqual(r, "inserted")
        t = self.store.get_term("綾小路")
        assert t is not None
        self.assertEqual(t.target, "绫小路")
        self.assertEqual(t.gender, "male")

    def test_terms_in_matches_alias(self):
        self.store.upsert_term(
            GlossaryTerm(source="綾小路", target="绫小路", aliases=["綾小路くん"])
        )
        hits = self.store.terms_in(
            self.store.all_terms(), "「おはよう、綾小路くん」と堀北が言った。"
        )
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].source, "綾小路")

    def test_terms_in_normalizes_case_and_character_width(self):
        self.store.upsert_term(GlossaryTerm(source="OpenAI", target="开放人工智能"))
        self.store.upsert_term(GlossaryTerm(source="ＡＢＣ", target="ABC 组织"))

        hits = self.store.terms_in(self.store.all_terms(), "openai 与 ABC")

        self.assertEqual(
            {term.source for term in hits},
            {"OpenAI", "ＡＢＣ"},
        )

    def test_terms_in_longest_match_shadows_substring_hits(self):
        """A longer matched term shadows overlapping substring hits in the same span."""
        self.store.upsert_term(GlossaryTerm(source="東京", target="东京"))
        self.store.upsert_term(GlossaryTerm(source="大学", target="大学"))
        self.store.upsert_term(GlossaryTerm(source="東京大学", target="东京大学"))

        hits = self.store.terms_in(self.store.all_terms(), "東京大学の前を歩く。")

        self.assertEqual([term.source for term in hits], ["東京大学"])

    def test_terms_in_longest_match_keeps_standalone_occurrences(self):
        """Shadowing hides only covered spans; a standalone occurrence still injects."""
        self.store.upsert_term(GlossaryTerm(source="東京", target="东京"))
        self.store.upsert_term(GlossaryTerm(source="東京大学", target="东京大学"))

        hits = self.store.terms_in(self.store.all_terms(), "東京大学は東京にある。")

        self.assertEqual([term.source for term in hits], ["東京", "東京大学"])

    def test_terms_in_preserves_insertion_order_of_survivors(self):
        """Survivors keep the glossary's stable insertion order."""
        self.store.upsert_term(GlossaryTerm(source="大学", target="大学"))
        self.store.upsert_term(GlossaryTerm(source="早稲田", target="早稻田"))
        self.store.upsert_term(GlossaryTerm(source="東京大学", target="东京大学"))

        hits = self.store.terms_in(self.store.all_terms(), "東京大学と早稲田。")

        self.assertEqual([term.source for term in hits], ["早稲田", "東京大学"])

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
        self.assertEqual(self.store.terms_in(self.store.all_terms(), "夏帆は窓の外を見た。"), [])
        hits = self.store.terms_in(self.store.all_terms(), "「夏帆ちゃん」と母親が言った。")
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
        # An alternate translation preserves the established mapping and records a candidate.
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
        """Insertion order controls all_terms so new entries cannot disrupt prompt prefix
        caching.
        """
        # Insert terms in an order that differs from type/source sorting deliberately.
        self.store.upsert_term(
            GlossaryTerm(source="乙", target="Yi", type="term"),
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
        # Human resolution or same-target field merging must not change insertion position.
        self.assertTrue(self.store.resolve_term("乙", "Yi-updated"))
        terms = self.store.all_terms()
        self.assertEqual([term.source for term in terms], ["乙", "甲"])
        self.assertEqual(terms[0].target, "Yi-updated")
        # New terms may only append to the end.
        self.store.upsert_term(
            GlossaryTerm(source="丙", target="Bing", type=TYPE_PERSON),
            chapter=1,
        )
        self.assertEqual(
            [term.source for term in self.store.all_terms()],
            ["乙", "甲", "丙"],
        )

    def test_equivalent_spellings_conflict_instead_of_coexisting(self):
        """Full-width and half-width sources are one term: a different target conflicts and
        stays a single row instead of injecting contradictory translations.
        """
        self.store.upsert_term(GlossaryTerm(source="ＡＩ", target="人工智能"), chapter=1)
        result = self.store.upsert_term(
            GlossaryTerm(source="AI", target="artificial intelligence"), chapter=2
        )
        self.assertEqual(result, "conflict")
        terms = self.store.all_terms()
        self.assertEqual([term.source for term in terms], ["ＡＩ"])
        self.assertEqual(terms[0].status, "conflict")
        # Either spelling resolves to the single stored entry, keeping the display form.
        resolved = self.store.get_term("AI")
        assert resolved is not None
        self.assertEqual(resolved.source, "ＡＩ")
        conflicts = self.store.open_conflicts()
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["source"], "ＡＩ")
        self.assertEqual(conflicts[0]["proposed_target"], "artificial intelligence")

    def test_equivalent_spellings_same_target_merge_aliases(self):
        """A repeated term under an equivalent spelling merges fields instead of duplicating."""
        self.store.upsert_term(
            GlossaryTerm(source="ＡＩ", target="人工智能", aliases=["机器智能"]), chapter=1
        )
        result = self.store.upsert_term(
            GlossaryTerm(source="AI", target="人工智能", aliases=["人工智能体"]), chapter=2
        )
        self.assertEqual(result, "unchanged")
        terms = self.store.all_terms()
        self.assertEqual(len(terms), 1)
        self.assertEqual(terms[0].aliases, ["人工智能体", "机器智能"])
        self.assertEqual(self.store.stats(), {"terms": 1, "open_conflicts": 0})

    def test_normalized_lookup_resolution_and_injection_dedup(self):
        """Lookups, resolution and term injection work through any equivalent spelling."""
        self.store.upsert_term(GlossaryTerm(source="ＡＩ", target="人工智能"), chapter=1)
        self.store.upsert_term(GlossaryTerm(source="ａｉ", target="机器智能"), chapter=2)
        ai_term = self.store.get_term("AI")
        assert ai_term is not None
        self.assertEqual(ai_term.target, "人工智能")
        self.assertEqual([term.source for term in self.store.get_terms(["AI"])], ["ＡＩ"])
        self.assertTrue(self.store.resolve_term("AI", "人工智能"))
        ai_variant = self.store.get_term("ａｉ")
        assert ai_variant is not None
        self.assertEqual(ai_variant.status, "ok")
        # Injection stays unique: equivalent spellings hit one stored term.
        injected = GlossaryStore.terms_in(
            self.store.all_terms(), "The AI system and the ＡＩ system."
        )
        self.assertEqual([term.source for term in injected], ["ＡＩ"])
        self.store.mark_conflicts_resolved("AI")
        self.assertEqual(self.store.open_conflicts(), [])

    def test_legacy_database_migrates_equivalent_sources(self):
        """A pre-source_norm database migrates in place: equivalent rows merge and conflicts
        re-point to the surviving display source.
        """
        with tempfile.TemporaryDirectory() as directory:
            db_path = os.path.join(directory, "legacy.db")
            conn = sqlite3.connect(db_path)
            conn.executescript(
                """
                CREATE TABLE glossary (
                    source TEXT PRIMARY KEY, target TEXT NOT NULL, reading TEXT,
                    type TEXT, gender TEXT, aliases TEXT, first_chapter INTEGER,
                    note TEXT, status TEXT DEFAULT 'ok', updated_at REAL
                );
                CREATE TABLE term_conflicts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT,
                    existing_target TEXT, proposed_target TEXT, chapter INTEGER,
                    note TEXT, resolved INTEGER DEFAULT 0, created_at REAL
                );
                """
            )
            conn.execute(
                "INSERT INTO glossary (source, target, type, updated_at)"
                " VALUES ('ＡＩ', '人工智能', 'term', 1)"
            )
            conn.execute(
                "INSERT INTO glossary (source, target, type, updated_at)"
                " VALUES ('AI', 'artificial intelligence', 'term', 2)"
            )
            conn.execute(
                "INSERT INTO term_conflicts (source, existing_target, proposed_target, created_at)"
                " VALUES ('AI', 'artificial intelligence', 'machine intelligence', 3)"
            )
            conn.commit()
            conn.close()

            store = GlossaryStore(db_path)
            terms = store.all_terms()
            self.assertEqual([term.source for term in terms], ["ＡＩ"])
            self.assertEqual(terms[0].status, "conflict")
            conflicts = store.open_conflicts()
            self.assertEqual(
                sorted((c["source"], c["proposed_target"]) for c in conflicts),
                [
                    ("ＡＩ", "artificial intelligence"),
                    ("ＡＩ", "machine intelligence"),
                ],
            )
            indexes = {
                row[0]
                for row in store.conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
            }
            self.assertIn("idx_glossary_source_norm", indexes)
            # The migrated store keeps enforcing single normalized mappings.
            self.assertEqual(
                store.upsert_term(GlossaryTerm(source="ａｉ", target="机器智能"), chapter=3),
                "conflict",
            )
            store.close()

            reopened = GlossaryStore(db_path)
            self.assertEqual(len(reopened.all_terms()), 1)
            reopened.close()


if __name__ == "__main__":
    unittest.main()
