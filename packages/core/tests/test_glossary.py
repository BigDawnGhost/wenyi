"""Glossary tests."""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from wenyi_core.glossary.store import (
    TYPE_APPELLATION,
    TYPE_PERSON,
    GlossaryStore,
    GlossaryTerm,
    normalize_match_text,
    source_matches_normalized,
    source_matches_text,
    source_occurrence_spans,
)

# (source, text, expected) across scripts whose matching rules differ.
_MATCH_CASES = [
    # ASCII: case-insensitive, word boundaries.
    ("Ann", "Ann opened the door.", True),
    ("ANN", "ann opened the door.", True),
    ("Ann", "Anna opened the door.", False),
    ("Ann", "Joann waved.", False),
    ("R2-D2", "Then R2-D2 beeped.", True),
    # Full-width ASCII folds to ASCII under NFKC.
    ("ＡＢＣ", "abc corp", True),
    ("ABC", "ＡＢＣ社", True),
    # Latin with accents, including decomposed (NFD) spellings on either side.
    ("Zoë", "Then Zoë left.", True),
    ("Zoe\u0308", "Then Zoë left.", True),
    ("Zoë", "Then Zoe\u0308 left.", True),
    ("Zoë", "Zoëlla arrived.", False),
    ("Élodie", "élodie smiled.", True),
    # Greek and Cyrillic use word boundaries.
    ("Αθηνά", "Η Αθηνά μίλησε.", True),
    ("Αθηνά", "Η Αθηνάς μίλησε.", False),
    ("гад", "Этот гад снова пришёл.", True),
    ("гад", "Этот гадкий человек снова пришёл.", False),
    # CJK and kana match as substrings.
    ("綾小路", "綾小路くんが来た。", True),
    ("小路", "綾小路くんが来た。", True),
    ("堀北", "綾小路くんが来た。", False),
    # Embedded furigana markers are ignored on the text side.
    ("与り", "与〘あずか〙り知らぬ", True),
    ("綾小路", "綾〘あや〙小路〘のこうじ〙が来た", True),
    # Empty and whitespace-only sources never match.
    ("", "anything", False),
    ("   ", "anything", False),
    ("\u3000", "anything", False),
]


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

    def test_ascii_source_match_respects_word_boundaries(self):
        self.assertTrue(source_matches_text("Ann", "Ann opened the door."))
        self.assertTrue(source_matches_text("ANN", "ann opened the door."))
        self.assertFalse(source_matches_text("Ann", "Anna opened the door."))

    def test_source_matches_normalized_agrees_with_source_matches_text(self):
        for source, text, expected in _MATCH_CASES:
            with self.subTest(source=source, text=text):
                self.assertEqual(source_matches_text(source, text), expected)
                self.assertEqual(
                    source_matches_normalized(source, normalize_match_text(text)), expected
                )

    def test_source_occurrence_spans_follow_match_rules(self):
        text = normalize_match_text("Ann met Anna and ANN; 綾小路と綾小路")
        self.assertEqual(source_occurrence_spans("Ann", text), [(0, 3), (17, 20)])
        self.assertEqual(len(source_occurrence_spans("綾小路", text)), 2)
        self.assertEqual(source_occurrence_spans("  ", text), [])

    def test_terms_in_keeps_input_order_and_alias_rules(self):
        terms = [
            GlossaryTerm(source="Zoë", target="佐伊", type=TYPE_PERSON),
            GlossaryTerm(source="綾小路", target="绫小路", type=TYPE_PERSON, aliases=["清隆"]),
            GlossaryTerm(source="Ann", target="安", type=TYPE_PERSON),
            GlossaryTerm(
                source="夏帆ちゃん", target="小夏帆", type=TYPE_APPELLATION, aliases=["夏帆"]
            ),
            GlossaryTerm(source="Αθηνά", target="雅典娜", type=TYPE_PERSON, aliases=["  "]),
            GlossaryTerm(source="Bob", target="鲍勃", type=TYPE_PERSON, aliases=["Bobby"]),
        ]
        text = "ＡＮＮ saw 清隆 and 夏帆 with Zoe\u0308; Αθηνάς and Bobby were absent? No, Bobby."

        hits = GlossaryStore.terms_in(terms, text)

        # Input order is preserved, aliases match ordinary entities, appellations match only
        # by their full source, and blank aliases never match.
        self.assertEqual([term.source for term in hits], ["Zoë", "綾小路", "Ann", "Bob"])

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

    def test_context_dependent_status_merges_senses_and_renders_as_guidance(self):
        from wenyi_core.agents import prompts
        from wenyi_core.glossary.store import STATUS_CONTEXT_DEPENDENT

        self.store.upsert_term(GlossaryTerm(source="洗白", target="go legit"), chapter=0)
        self.assertEqual(
            self.store.upsert_term(GlossaryTerm(source="洗白", target="whitewash"), chapter=1),
            "conflict",
        )
        self.assertEqual(len(self.store.open_conflicts()), 1)

        self.assertTrue(
            self.store.resolve_term(
                "洗白",
                "go legit | whitewash | launder",
                status=STATUS_CONTEXT_DEPENDENT,
            )
        )
        self.store.mark_conflicts_resolved("洗白")
        term = self.store.get_term("洗白")
        assert term is not None
        self.assertEqual(term.status, STATUS_CONTEXT_DEPENDENT)
        self.assertEqual(term.target, "go legit | whitewash | launder")
        self.assertEqual(self.store.open_conflicts(), [])

        # Subsequent extractions append new senses instead of re-opening conflicts.
        self.assertEqual(
            self.store.upsert_term(GlossaryTerm(source="洗白", target="go legit"), chapter=2),
            "unchanged",
        )
        self.assertEqual(
            self.store.upsert_term(GlossaryTerm(source="洗白", target="rehabilitate"), chapter=3),
            "unchanged",
        )
        updated = self.store.get_term("洗白")
        assert updated is not None
        self.assertEqual(updated.status, STATUS_CONTEXT_DEPENDENT)
        self.assertEqual(updated.target, "go legit | whitewash | launder | rehabilitate")
        self.assertEqual(self.store.open_conflicts(), [])

        # Shared prompt renderer (used by Translator, Polisher, Reviewer, ReviewFixer) renders
        # context-dependent entries as guidance rather than a fixed "→" mapping.
        fixed = GlossaryTerm(source="少将", target="major general", type="term")
        rendered = prompts.render_glossary([fixed, updated], source_lang="zh")
        self.assertIn("- 少将 → major general(term)", rendered)
        self.assertIn(
            "- 洗白: go legit | whitewash | launder | rehabilitate"
            "(term, context-dependent; choose by context)",
            rendered,
        )
        self.assertNotIn("- 洗白 →", rendered)


if __name__ == "__main__":
    unittest.main()
